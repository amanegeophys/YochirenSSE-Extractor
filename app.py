import base64
import csv
import hashlib
import io
import json
import os
import re
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import fitz
from openai import OpenAI
from PIL import Image, ImageTk

STORE_LOCK = threading.RLock()

# =========================
# Config / Constants
# =========================
APP_TITLE = "YochirenSSE-Extractor"
APP_DIR = Path(__file__).resolve().parent
DEFAULT_DPI = 350
RESULT_DIR = APP_DIR / "data" / "session"
CROP_DIR = RESULT_DIR / "crops"
DOWNLOAD_DIR = APP_DIR / "downloads"
MAX_DISPLAY_WIDTH = 1500
MAX_DISPLAY_HEIGHT = 1080
MODEL_DEFAULT = "gpt-5.4-mini"
COLOR_BG = "#edf2f1"
COLOR_PANEL = "#f8fbfa"
COLOR_SURFACE = "#ffffff"
COLOR_INK = "#1d2b34"
COLOR_MUTED = "#647780"
COLOR_BORDER = "#d7e1de"
COLOR_ACCENT = "#128c7e"
COLOR_ACCENT_DARK = "#0f6f65"
COLOR_ACCENT_SOFT = "#e4f3f0"
COLOR_WARN = "#b7791f"
COLOR_DANGER = "#b94a48"
COLOR_CANVAS = "#172026"

RESULT_DIR.mkdir(parents=True, exist_ok=True)
CROP_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

FIELDS = [
    "timestamp",
    "lat",
    "lon",
    "dep",
    "len",
    "wid",
    "strike",
    "dip",
    "rake",
    "slip",
    "mw",
]

# NOTE: 「longitude」の綴りは既存CSV互換のため残しています
CSV_FIELDS = [
    "timestamp",
    "latitude",
    "longitude",
    "depth",
    "length",
    "width",
    "strike",
    "dip",
    "rake",
    "slip",
    "mw",
    "uid",
    "pdf_path",
    "page_index",
    "dpi",
    "img_path",
]

HELP_TEXT = """使い方

1. File > Open PDF... からPDFを開きます。
2. 左側のPDFビューで図をドラッグして範囲選択します。
3. 選択した範囲は自動で切り出され、右側にフォームが追加されます。
4. GPT解析が終わると各項目に値が入るので、必要に応じて修正して Save を押します。
5. 現在のページ上の内容をまとめて保存したい場合は Save All on Page を使います。

キー操作

Left: 前のページ
Right: 次のページ
Enter: 現在の内容を保存
Space: 幅にフィット
Ctrl + Mouse Wheel: ズーム
Mouse Wheel: スクロール

補足

- 右側の Delete で選択した図の記録を削除できます。
- 保存データは data/session 配下に JSONL / CSV として書き出されます。
"""


@dataclass
class CropRecord:
    uid: str
    pdf_path: str
    page_index: int
    dpi: int
    crop_rect: tuple[float, float, float, float]
    img_path: str
    ts: str
    fields: dict[str, object]
    model_name: str = ""
    raw_text: str = ""

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, ensure_ascii=False)


def _session_safe_name_from_pdf(pdf_path: str) -> str:
    p = Path(pdf_path)
    stem = p.stem
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", stem) or "pdf"


def _session_prefix_from_pdf(pdf_path: str) -> str:
    p = Path(pdf_path)
    safe = _session_safe_name_from_pdf(pdf_path)
    digest = hashlib.sha1(p.name.encode("utf-8")).hexdigest()[:8]
    return f"{safe}_{digest}"


def jsonl_path_for(pdf_path: str) -> Path:
    return RESULT_DIR / f"{_session_prefix_from_pdf(pdf_path)}.jsonl"


def csv_path_for(pdf_path: str) -> Path:
    return RESULT_DIR / f"{_session_prefix_from_pdf(pdf_path)}.csv"


def jsonl_paths_for_read(pdf_path: str) -> list[Path]:
    primary = jsonl_path_for(pdf_path)
    safe = _session_safe_name_from_pdf(pdf_path)
    paths = [primary] if primary.exists() else []
    paths.extend(sorted(p for p in RESULT_DIR.glob(f"{safe}_*.jsonl") if p != primary))
    return paths


def now_ts() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_dotenv_if_exists(path: Path = APP_DIR / ".env") -> None:
    """Minimal .env loader so beginners do not need another dependency."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_openai_api_key() -> str:
    load_dotenv_if_exists()
    return os.environ.get("OPENAI_API_KEY", "").strip()


def app_relative_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(APP_DIR))
    except ValueError:
        return str(path.resolve())


def path_for_display(path_text: str) -> Path:
    path = Path(path_text)
    if not path.is_absolute():
        return APP_DIR / path
    return path


def migrated_crop_path(path_text: str) -> str:
    path = path_for_display(path_text)
    if path.exists():
        return app_relative_path(path)
    fallback = CROP_DIR / Path(path_text).name
    if fallback.exists():
        return app_relative_path(fallback)
    return path_text


def b64_png(image_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(image_bytes).decode("utf-8")


def _atomic_write_jsonl(path: Path, rows: list[dict]):
    """JSONLをtmpに全行書いてから原子置換。クラッシュ/同時書き込み対策。"""
    tmp = Path(str(path) + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False))
            f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # Windows/Unix ともに原子的に置換


def _atomic_write_csv(path: Path, rows: list[dict], header: list[str]):
    """CSVも同様にアトミックに置換。"""
    tmp = Path(str(path) + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _records_to_csv_rows(records: dict[str, "CropRecord"]) -> list[dict]:
    out = []
    for rec in records.values():
        flat = {
            "uid": rec.uid,
            "pdf_path": rec.pdf_path,
            "page_index": str(rec.page_index),
            "dpi": str(rec.dpi),
            "img_path": rec.img_path,
            "timestamp": str(rec.fields.get("timestamp")),
            "latitude": str(rec.fields.get("lat")),
            "longitude": str(rec.fields.get("lon")),  # 既存互換
            "depth": str(rec.fields.get("dep")),
            "length": str(rec.fields.get("len")),
            "width": str(rec.fields.get("wid")),
            "strike": str(rec.fields.get("strike")),
            "dip": str(rec.fields.get("dip")),
            "rake": str(rec.fields.get("rake")),
            "slip": str(rec.fields.get("slip")),
            "mw": str(rec.fields.get("mw")),
        }
        out.append({k: flat.get(k, "") for k in CSV_FIELDS})
    # 見やすさのために一定順で保存（任意）
    out.sort(key=lambda r: (r["pdf_path"], int(r["page_index"]), r["img_path"]))
    return out


def persist_all(records: dict[str, "CropRecord"], pdf_path: str | None):
    """アプリ内の唯一の真実(self.records)からJSONL/CSVをアトミックに同時更新。"""
    if not pdf_path:
        return  # PDF未選択なら何もしない
    json_rows = []
    for rec in records.values():
        d = asdict(rec)
        # dataclass -> jsonl で型崩れ防止（tupleはlistにしてもOKだがasdictで十分）
        json_rows.append(d)
    # 並行書き込みをロックで串刺し
    jpath = jsonl_path_for(pdf_path)
    cpath = csv_path_for(pdf_path)

    with STORE_LOCK:
        _atomic_write_jsonl(jpath, json_rows)
        _atomic_write_csv(cpath, _records_to_csv_rows(records), CSV_FIELDS)


def call_gpt_extract(image_bytes: bytes, model: str) -> tuple[dict, str]:
    api_key = get_openai_api_key()
    if not api_key:
        return {}, "ERROR: OPENAI_API_KEY is not set. Create .env or set an environment variable."

    client = OpenAI(api_key=api_key)

    instruction = (
        "画像はスロースリップの図(b1)などです。凡例やキャプションから次の量を抽出し、単位を外して数値のみを返してください。\n"
        "ただし、slipの単位はmmとして数値を返してください。\n"
        "keys: timestamp, lat, lon, dep(or depth), len(or leng), wid, strike, dip, rake, slip, mw(or M_w).\n"
        "見当たらない値は null。JSON以外の文字は出力しない。\n"
        "timestampについては、午前または午後と書かれている部分はそれぞれAMまたはPMに変換してください。\n"
        "timestampが明示的に書かれていない場合は、キャプションの深部低周波微動の発生期間をもとに作成してください。\n"
        '出力例：{"timestamp":"2016/4/18-19","lat":33.59,"lon":132.66,"dep":31,"len":38,"wid":38,"strike":189,"dip":11,"rake":64,"slip":16,"mw":5.9}\n'
    )

    img_url = b64_png(image_bytes)

    try:
        resp = client.responses.create(
            model=model,
            reasoning={"effort": "low"},
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": instruction},
                        {"type": "input_image", "image_url": f"{img_url}"},
                    ],
                },
            ],
            text={"verbosity": "low"},
        )
        txt = resp.output_text
    except Exception as e:
        return {}, f"ERROR: {e}"

    # JSONだけ抜き出す保険（万一何か混ざった場合）
    s = txt.strip()
    if not (s.startswith("{") and s.endswith("}")):
        try:
            start = s.index("{")
            end = s.rindex("}") + 1
            s = s[start:end]
        except Exception:
            return {}, txt

    try:
        data = json.loads(s)
    except Exception:
        return {}, txt

    for k in ["lon", "dep", "wid", "strike", "dip", "rake", "slip", "mw", "lat", "len"]:
        v = data.get(k)
        if v is None:
            continue
        try:
            data[k] = float(v)
        except Exception:
            data[k] = None

    if "timestamp" in data and isinstance(data["timestamp"], str):
        data["timestamp"] = data["timestamp"].strip()
    return data, txt


def load_existing_for_pdf(pdf_path: str) -> dict[str, CropRecord]:
    data: dict[str, CropRecord] = {}
    for jpath in jsonl_paths_for_read(pdf_path):
        for line in jpath.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
                stored_pdf_path = obj["pdf_path"]
                uid = obj["uid"]
                if uid.startswith(f"{stored_pdf_path}|"):
                    uid = f"{pdf_path}|{uid[len(stored_pdf_path) + 1:]}"

                rec = CropRecord(
                    uid=uid,
                    pdf_path=pdf_path,
                    page_index=int(obj["page_index"]),
                    dpi=int(obj.get("dpi", DEFAULT_DPI)),
                    crop_rect=tuple(obj["crop_rect"]),
                    img_path=migrated_crop_path(obj["img_path"]),
                    ts=obj["ts"],
                    fields=obj.get("fields", {k: None for k in FIELDS}),
                    model_name=obj.get("model_name", ""),
                    raw_text=obj.get("raw_text", ""),
                )
                data[rec.uid] = rec
            except Exception:
                continue
    return data


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry(f"{MAX_DISPLAY_WIDTH}x{MAX_DISPLAY_HEIGHT}")
        self.minsize(1120, 760)
        self.configure(bg=COLOR_BG)

        # PDF
        self.pdf_path: str | None = None
        self.doc: fitz.Document | None = None
        self.page_index: int = 0
        self.dpi: int = DEFAULT_DPI
        self.model_name: str = MODEL_DEFAULT

        # 表示状態
        self.page_pix_size: tuple[int, int] = (1, 1)  # レンダリング後のピクセル
        self.page_disp_size: tuple[int, int] = (1, 1)  # 実際に表示しているサイズ
        self._base_img: Image.Image | None = None  # DPIでレンダした元画像
        self._img_ref: ImageTk.PhotoImage | None = None
        self._rect_id: int | None = None
        self._drag_start: tuple[int, int] | None = None

        # ズーム
        self.zoom: float = 1.0
        self.fit_width: bool = True  # 初期は幅フィット

        # データ
        self.records: dict[str, CropRecord] = {}
        self.form_by_uid: dict[str, ttk.LabelFrame] = {}

        # 並列実行（GPT呼び出しを非同期化）
        self.executor = ThreadPoolExecutor(max_workers=5)
        self.pending_lock = threading.Lock()
        self.pending_jobs: set[str] = set()  # uid集合
        self.help_window: tk.Toplevel | None = None

        self._configure_style()
        self._build_ui()
        self._bind_keys()

    # ---------- UI ----------
    def _configure_style(self):
        self.style = ttk.Style(self)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass

        self.option_add("*Font", ("TkDefaultFont", 10))
        self.option_add("*Menu.background", COLOR_SURFACE)
        self.option_add("*Menu.foreground", COLOR_INK)
        self.option_add("*Menu.activeBackground", "#dcecea")
        self.option_add("*Menu.activeForeground", COLOR_INK)

        self.style.configure(".", font=("TkDefaultFont", 10))
        self.style.configure("App.TFrame", background=COLOR_BG)
        self.style.configure("Toolbar.TFrame", background=COLOR_SURFACE)
        self.style.configure("Panel.TFrame", background=COLOR_PANEL)
        self.style.configure("Card.TFrame", background=COLOR_SURFACE)
        self.style.configure("TLabel", background=COLOR_SURFACE, foreground=COLOR_INK)
        self.style.configure(
            "Title.TLabel",
            background=COLOR_SURFACE,
            foreground=COLOR_INK,
            font=("TkDefaultFont", 15, "bold"),
        )
        self.style.configure(
            "Muted.TLabel", background=COLOR_SURFACE, foreground=COLOR_MUTED
        )
        self.style.configure(
            "PanelTitle.TLabel",
            background=COLOR_PANEL,
            foreground=COLOR_INK,
            font=("TkDefaultFont", 12, "bold"),
            padding=(0, 0, 0, 8),
        )
        self.style.configure(
            "Field.TLabel", background=COLOR_SURFACE, foreground=COLOR_MUTED
        )
        self.style.configure(
            "Page.TLabel",
            background=COLOR_SURFACE,
            foreground=COLOR_ACCENT_DARK,
            font=("TkDefaultFont", 10, "bold"),
        )
        self.style.configure(
            "Status.TLabel",
            background="#21323b",
            foreground="#e8f3f1",
            padding=(12, 6),
            relief="flat",
        )
        self.style.configure("TEntry", padding=(8, 5), fieldbackground="#ffffff")
        self.style.configure("TSpinbox", padding=(7, 4), fieldbackground="#ffffff")
        self.style.configure(
            "TButton",
            padding=(10, 6),
            background="#ffffff",
            foreground=COLOR_INK,
            bordercolor=COLOR_BORDER,
            focusthickness=0,
        )
        self.style.map("TButton", background=[("active", "#eef6f4")])
        self.style.configure(
            "Accent.TButton",
            background=COLOR_ACCENT,
            foreground="#ffffff",
            bordercolor=COLOR_ACCENT,
            focusthickness=0,
        )
        self.style.map(
            "Accent.TButton",
            background=[("active", COLOR_ACCENT_DARK), ("disabled", "#9cb8b4")],
            foreground=[("disabled", "#eef4f3")],
        )
        self.style.configure(
            "Danger.TButton",
            background="#fff4f2",
            foreground=COLOR_DANGER,
            bordercolor="#e8b9b5",
        )
        self.style.map("Danger.TButton", background=[("active", "#ffe7e3")])
        self.style.configure(
            "Crop.TLabelframe",
            background=COLOR_SURFACE,
            bordercolor=COLOR_BORDER,
            relief="solid",
            padding=(12, 10),
        )
        self.style.configure(
            "Crop.TLabelframe.Label",
            background=COLOR_SURFACE,
            foreground=COLOR_ACCENT_DARK,
            font=("TkDefaultFont", 10, "bold"),
        )
        self.style.configure(
            "Thumb.TLabel",
            background="#f1f6f5",
            bordercolor=COLOR_BORDER,
            relief="solid",
            padding=6,
        )

    def _build_ui(self):
        menubar = tk.Menu(self)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Open PDF...", command=self.open_pdf)
        filemenu.add_command(label="Usage / Keys", command=self.open_help_window)
        filemenu.add_separator()
        filemenu.add_command(label="Exit", command=self.destroy)
        menubar.add_cascade(label="File", menu=filemenu)
        self.config(menu=menubar)

        toolbar = ttk.Frame(self, style="Toolbar.TFrame", padding=(14, 10))
        toolbar.pack(side=tk.TOP, fill=tk.X)

        title_area = ttk.Frame(toolbar, style="Toolbar.TFrame")
        title_area.pack(side=tk.LEFT)
        ttk.Label(
            title_area,
            text="YochirenSSE-Extractor",
            style="Title.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            title_area,
            text="PDF figures to structured SSE records",
            style="Muted.TLabel",
        ).pack(anchor="w")

        controls = ttk.Frame(toolbar, style="Toolbar.TFrame")
        controls.pack(side=tk.RIGHT)

        ttk.Label(controls, text="DPI:", style="Muted.TLabel").pack(side=tk.LEFT)
        self.dpi_var = tk.IntVar(value=self.dpi)
        dpi_entry = ttk.Spinbox(
            controls,
            from_=120,
            to=600,
            increment=10,
            textvariable=self.dpi_var,
            width=6,
        )
        dpi_entry.pack(side=tk.LEFT, padx=(5, 12))

        ttk.Label(controls, text="Model:", style="Muted.TLabel").pack(side=tk.LEFT)
        self.model_var = tk.StringVar(value=self.model_name)
        ttk.Entry(controls, textvariable=self.model_var, width=20).pack(
            side=tk.LEFT, padx=(5, 12)
        )

        ttk.Separator(controls, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=(0, 12)
        )

        ttk.Button(controls, text="Prev", command=self.prev_page).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        self.page_label = ttk.Label(controls, text="Page -/-", style="Page.TLabel")
        self.page_label.pack(side=tk.LEFT, padx=8)
        ttk.Button(controls, text="Next", command=self.next_page).pack(
            side=tk.LEFT, padx=(4, 12)
        )

        ttk.Separator(controls, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=(0, 12)
        )

        # Zoom controls
        ttk.Button(
            controls, text="-", width=3, command=lambda: self._zoom_step(0.9)
        ).pack(side=tk.LEFT)
        ttk.Button(controls, text="100%", command=self._zoom_reset).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(controls, text="Fit Width", command=self._zoom_fit_width).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(
            controls, text="+", width=3, command=lambda: self._zoom_step(1.1)
        ).pack(side=tk.LEFT, padx=(0, 12))

        ttk.Button(
            controls,
            text="Save Page",
            command=self.save_all_on_page,
            style="Accent.TButton",
        ).pack(side=tk.LEFT)

        body = ttk.Frame(self, style="App.TFrame", padding=(12, 0, 12, 12))
        body.pack(fill=tk.BOTH, expand=True)

        self.main = ttk.Panedwindow(body, orient=tk.HORIZONTAL)
        self.main.pack(fill=tk.BOTH, expand=True)

        # 左：PDFキャンバス
        left = ttk.Frame(self.main, style="Panel.TFrame")
        self.main.add(left, weight=3)

        vsb_left = ttk.Scrollbar(left, orient=tk.VERTICAL)
        vsb_left.pack(side=tk.RIGHT, fill=tk.Y)

        hsb_left = ttk.Scrollbar(left, orient=tk.HORIZONTAL)
        hsb_left.pack(side=tk.BOTTOM, fill=tk.X)

        self.canvas = tk.Canvas(
            left,
            bg=COLOR_CANVAS,
            highlightthickness=0,
            yscrollcommand=vsb_left.set,
            xscrollcommand=hsb_left.set,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        vsb_left.config(command=self.canvas.yview)
        hsb_left.config(command=self.canvas.xview)

        self.canvas.bind("<Configure>", lambda e: self._update_canvas_image())
        self.canvas.bind("<ButtonPress-1>", self.on_mouse_down)
        self.canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_mouse_up)

        # スクロール（Linux/Win両対応）
        # self.canvas.bind("<MouseWheel>", self._on_mousewheel_left)  # Windows
        self.canvas.bind("<Button-4>", self._on_mousewheel_linux_left)  # Linux up
        self.canvas.bind("<Button-5>", self._on_mousewheel_linux_left)  # Linux down
        # Ctrl+ホイールでズーム
        # self.canvas.bind("<Control-MouseWheel>", self._on_ctrl_wheel)
        self.canvas.bind("<Control-Button-4>", self._on_ctrl_wheel_linux)
        self.canvas.bind("<Control-Button-5>", self._on_ctrl_wheel_linux)

        # 右：フォーム
        right = ttk.Frame(self.main, style="Panel.TFrame", padding=(10, 10, 0, 0))
        self.main.add(right, weight=2)

        ttk.Label(right, text="Extracted Figures", style="PanelTitle.TLabel").pack(
            side=tk.TOP, anchor="w"
        )
        self.scroll_canvas = tk.Canvas(right, bg=COLOR_PANEL, highlightthickness=0)
        self.scroll_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb_right = ttk.Scrollbar(
            right, orient=tk.VERTICAL, command=self.scroll_canvas.yview
        )
        vsb_right.pack(side=tk.RIGHT, fill=tk.Y)
        self.scroll_canvas.configure(yscrollcommand=vsb_right.set)
        self.scroll_canvas.bind("<MouseWheel>", self._on_mousewheel_right)  # Windows
        self.scroll_canvas.bind(
            "<Button-4>", self._on_mousewheel_linux_right
        )  # Linux up
        self.scroll_canvas.bind(
            "<Button-5>", self._on_mousewheel_linux_right
        )  # Linux down
        self.form_frame = ttk.Frame(self.scroll_canvas, style="Panel.TFrame")
        self.window_id = self.scroll_canvas.create_window(
            (0, 0), window=self.form_frame, anchor="nw"
        )
        self.form_frame.bind(
            "<Configure>",
            lambda e: self.scroll_canvas.configure(
                scrollregion=self.scroll_canvas.bbox("all")
            ),
        )
        self.scroll_canvas.bind(
            "<Configure>",
            lambda e: self.scroll_canvas.itemconfigure(self.window_id, width=e.width),
        )

        self.status = tk.StringVar(value="Ready")
        self.statusbar = ttk.Label(
            self, textvariable=self.status, anchor="w", style="Status.TLabel"
        )
        self.statusbar.pack(side=tk.BOTTOM, fill=tk.X)

    def _bind_keys(self):
        self.bind("<Left>", self._on_left_key)
        self.bind("<Right>", self._on_right_key)
        self.bind("<Return>", self._on_return_key)
        self.bind("<space>", self._on_space_key)

    def _focus_is_text_input(self) -> bool:
        widget = self.focus_get()
        return isinstance(
            widget, (tk.Entry, tk.Spinbox, tk.Text, ttk.Entry, ttk.Spinbox)
        )

    def _on_left_key(self, _event):
        if self._focus_is_text_input():
            return None
        self.prev_page()
        return "break"

    def _on_right_key(self, _event):
        if self._focus_is_text_input():
            return None
        self.next_page()
        return "break"

    def _on_return_key(self, _event):
        if self._focus_is_text_input():
            return None
        self.save_all_on_page()
        return "break"

    def _on_space_key(self, _event):
        if self._focus_is_text_input():
            return None
        self._zoom_fit_width()
        return "break"

    # ---------- ファイル ----------
    def open_pdf(self):
        path = filedialog.askopenfilename(
            title="Open PDF",
            initialdir=DOWNLOAD_DIR if DOWNLOAD_DIR.exists() else APP_DIR,
            filetypes=[("PDF", "*.pdf")],
        )
        if not path:
            return

        # すでに別PDFを開いていて records がある場合は、先に保存しておく
        if self.pdf_path and self.records:
            try:
                persist_all(self.records, self.pdf_path)
            except Exception as e:
                messagebox.showwarning(
                    "Warning",
                    f"Previous PDF results could not be saved:\n{e}",
                )

        try:
            self.doc = fitz.open(path)
            self.pdf_path = str(Path(path).resolve())
            self.page_index = 0

            # このPDF用のJSONLを読み込む（なければ空）
            self.records = load_existing_for_pdf(self.pdf_path)

            self._render_page()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open PDF: {e}")

    def open_help_window(self):
        if self.help_window and self.help_window.winfo_exists():
            self.help_window.lift()
            self.help_window.focus_force()
            return

        win = tk.Toplevel(self)
        win.title("Usage / Keys")
        win.geometry("560x420")
        win.transient(self)
        self.help_window = win

        win.configure(bg=COLOR_BG)
        container = ttk.Frame(win, padding=12, style="App.TFrame")
        container.pack(fill=tk.BOTH, expand=True)

        text = tk.Text(
            container,
            wrap="word",
            height=20,
            bg=COLOR_SURFACE,
            fg=COLOR_INK,
            insertbackground=COLOR_ACCENT,
            relief="flat",
            padx=12,
            pady=12,
        )
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL, command=text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        text.configure(yscrollcommand=scrollbar.set)
        text.insert("1.0", HELP_TEXT)
        text.configure(state="disabled")

        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 12))
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.bind("<Escape>", lambda e: win.destroy())

    # ---------- レンダリング ----------
    def _render_page(self):
        if not self.doc:
            return
        self.dpi = int(self.dpi_var.get())
        self.model_name = self.model_var.get().strip()

        page = self.doc[self.page_index]
        mat = fitz.Matrix(self.dpi / 72, self.dpi / 72)
        pix = page.get_pixmap(matrix=mat, alpha=False)

        w_pix, h_pix = pix.width, pix.height
        self.page_pix_size = (w_pix, h_pix)

        # DPIで一旦ベース画像を作成（ズームは別段階で適用）
        self._base_img = Image.open(io.BytesIO(pix.tobytes("png")))
        self._update_canvas_image()  # 実表示とスクロール領域を更新

        self.page_label.config(
            text=f"Page {self.page_index + 1} / {self.doc.page_count}"
        )
        self.status.set("Ready, Drag to select a figure")
        self.update_idletasks()

        self._draw_saved_rects_on_canvas()
        self._refresh_forms_for_page()

    def _update_canvas_image(self):
        """キャンバスのサイズ/ズームに応じて表示を更新"""
        if self._base_img is None:
            return
        canvas_w = max(1, self.canvas.winfo_width())
        w_pix, h_pix = self.page_pix_size

        # 幅フィット時の基準倍率
        fit_scale = canvas_w / w_pix if self.fit_width else 1.0
        scale = fit_scale * self.zoom
        w_disp, h_disp = int(w_pix * scale), int(h_pix * scale)

        # ImageTkへ変換
        img = self._base_img
        if (img.width, img.height) != (w_disp, h_disp):
            img = img.resize((w_disp, h_disp), Image.LANCZOS)

        self.page_disp_size = (w_disp, h_disp)
        self._img_ref = ImageTk.PhotoImage(img)

        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self._img_ref, anchor=tk.NW, tags="page")
        self.canvas.configure(scrollregion=(0, 0, w_disp, h_disp))
        self._draw_saved_rects_on_canvas()

    def _draw_saved_rects_on_canvas(self):
        self.canvas.delete("saved")
        if not self.doc or not self.pdf_path:
            return
        w_pix, h_pix = self.page_pix_size
        w_disp, h_disp = self.page_disp_size
        px_per_pt = self.dpi / 72.0

        for rec in self.records.values():
            if rec.pdf_path != self.pdf_path or rec.page_index != self.page_index:
                continue
            x0, y0, x1, y1 = rec.crop_rect

            rx0 = x0 * px_per_pt
            ry0 = y0 * px_per_pt
            rx1 = x1 * px_per_pt
            ry1 = y1 * px_per_pt

            x0_disp = rx0 * (w_disp / w_pix)
            y0_disp = ry0 * (h_disp / h_pix)
            x1_disp = rx1 * (w_disp / w_pix)
            y1_disp = ry1 * (h_disp / h_pix)
            self.canvas.create_rectangle(
                x0_disp,
                y0_disp,
                x1_disp,
                y1_disp,
                outline="#00FFD5",
                width=2,
                tags="saved",
            )

    # ---------- ページ移動 ----------
    def prev_page(self):
        if not self.doc:
            return
        self.page_index = (self.page_index - 1) % self.doc.page_count
        self._render_page()

    def next_page(self):
        if not self.doc:
            return
        self.page_index = (self.page_index + 1) % self.doc.page_count
        self._render_page()

    # ---------- スクロール/ズーム ----------
    def _on_mousewheel_left(self, e):
        # 縦スクロール
        delta = -1 if e.delta > 0 else 1 if e.delta < 0 else 0
        self.canvas.yview_scroll(delta, "units")

    def _on_mousewheel_linux_left(self, e):
        if e.num == 4:
            self.canvas.yview_scroll(-1, "units")
        elif e.num == 5:
            self.canvas.yview_scroll(1, "units")

    def _on_mousewheel_right(self, e):
        # 縦スクロール
        delta = -1 if e.delta > 0 else 1 if e.delta < 0 else 0
        self.scroll_canvas.yview_scroll(delta, "units")

    def _on_mousewheel_linux_right(self, e):
        if e.num == 4:
            self.scroll_canvas.yview_scroll(-1, "units")
        elif e.num == 5:
            self.scroll_canvas.yview_scroll(1, "units")

    def _on_ctrl_wheel(self, e):
        # self.fit_width = False
        if e.delta > 0:
            self._zoom_step(1.1)
        else:
            self._zoom_step(0.9)

    def _on_ctrl_wheel_linux(self, e):
        # self.fit_width = False
        if e.num == 4:
            self._zoom_step(1.1)
        elif e.num == 5:
            self._zoom_step(0.9)

    def _zoom_step(self, factor: float):
        self.zoom = min(8.0, self.zoom * factor)
        self._update_canvas_image()

    def _zoom_reset(self):
        self.fit_width = False
        self.zoom = 1.0
        self._update_canvas_image()

    def _zoom_fit_width(self):
        self.fit_width = True
        self.zoom = 1.0
        self._update_canvas_image()

    # ---------- マウス操作 ----------
    def on_mouse_down(self, e):
        if self.doc is None:
            return
        cx = int(self.canvas.canvasx(e.x))
        cy = int(self.canvas.canvasy(e.y))
        self._drag_start = (cx, cy)
        if self._rect_id:
            self.canvas.delete(self._rect_id)
            self._rect_id = None

    def on_mouse_drag(self, e):
        if self.doc is None:
            return

        if not self._drag_start:
            return
        x0, y0 = self._drag_start
        x1 = int(self.canvas.canvasx(e.x))
        y1 = int(self.canvas.canvasy(e.y))
        if self._rect_id is None:
            self._rect_id = self.canvas.create_rectangle(
                x0, y0, x1, y1, outline="#FF5A00", width=2, tags="current_rect"
            )
        else:
            self.canvas.coords(self._rect_id, x0, y0, x1, y1)

    def on_mouse_up(self, e):
        if self.doc is None:
            return
        if not self._drag_start:
            return
        x0, y0 = self._drag_start
        x1 = int(self.canvas.canvasx(e.x))
        y1 = int(self.canvas.canvasy(e.y))
        self._drag_start = None

        if self._rect_id is None:
            return

        x0, x1 = sorted([x0, x1])
        y0, y1 = sorted([y0, y1])

        if (x1 - x0) < 8 or (y1 - y0) < 8:
            self.canvas.delete(self._rect_id)
            self._rect_id = None
            return

        rect_pt = self._display_rect_to_pdf_pt((x0, y0, x1, y1))
        self.canvas.itemconfig(self._rect_id, outline="#00C853")
        self.status.set("Cropping... (you can continue selecting)")

        # 非同期解析を開始（UIはブロックしない）
        self._start_async_crop(rect_pt)
        self._rect_id = None

    def _display_rect_to_pdf_pt(
        self, r_disp: tuple[int, int, int, int]
    ) -> tuple[float, float, float, float]:
        (dx0, dy0, dx1, dy1) = r_disp
        w_pix, h_pix = self.page_pix_size
        w_disp, h_disp = self.page_disp_size
        px_per_pt = self.dpi / 72.0

        rx0 = dx0 * (w_pix / w_disp)
        ry0 = dy0 * (h_pix / h_disp)
        rx1 = dx1 * (w_pix / w_disp)
        ry1 = dy1 * (h_pix / h_disp)

        x0 = rx0 / px_per_pt
        y0 = ry0 / px_per_pt
        x1 = rx1 / px_per_pt
        y1 = ry1 / px_per_pt

        page_rect = self.doc[self.page_index].rect
        rect = fitz.Rect(x0, y0, x1, y1) & page_rect
        return (rect.x0, rect.y0, rect.x1, rect.y1)

    # ---------- 非同期クロップ＋GPT ----------
    def _start_async_crop(self, rect_pt: tuple[float, float, float, float]):
        page = self.doc[self.page_index]
        x0, y0, x1, y1 = rect_pt
        clip = fitz.Rect(x0, y0, x1, y1)

        base = f"p{self.page_index + 1:03d}_{int(x0)}_{int(y0)}_{int(x1)}_{int(y1)}_{self.dpi}dpi.png"
        out_path = (CROP_DIR / base).resolve()

        pix = page.get_pixmap(
            matrix=fitz.Matrix(self.dpi / 72, self.dpi / 72), clip=clip, alpha=False
        )
        png_bytes = pix.tobytes("png")
        out_path.write_bytes(png_bytes)

        uid = f"{self.pdf_path}|{self.page_index}|{x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f}"
        rec = CropRecord(
            uid=uid,
            pdf_path=self.pdf_path,
            page_index=self.page_index,
            dpi=self.dpi,
            crop_rect=(x0, y0, x1, y1),
            img_path=app_relative_path(out_path),
            ts=now_ts(),
            fields={k: None for k in FIELDS},
            model_name=self.model_name,
            raw_text="",
        )

        self.records[uid] = rec
        self._append_form_for_record(rec, pending=True)

        # 非同期ジョブ投入
        with self.pending_lock:
            self.pending_jobs.add(uid)

        def worker():
            data, txt = call_gpt_extract(
                image_bytes=png_bytes, model=self.model_name or MODEL_DEFAULT
            )
            return (uid, data, txt)

        def done_callback(fut):
            try:
                uid_ret, data, txt = fut.result()
            except Exception as e:
                uid_ret, data, txt = uid, {}, f"ERROR: {e}"
            self.after(0, lambda: self._on_gpt_done(uid_ret, data, txt))

        future = self.executor.submit(worker)
        future.add_done_callback(done_callback)

    def _on_gpt_done(self, uid: str, data: dict, txt: str):
        with self.pending_lock:
            self.pending_jobs.discard(uid)
        rec = self.records.get(uid)
        if not rec:
            return
        # 値を反映
        for k in FIELDS:
            if k == "timestamp":
                rec.fields[k] = (
                    (data.get(k) or "").strip()
                    if isinstance(data.get(k), str)
                    else data.get(k)
                )
            else:
                v = data.get(k)
                try:
                    rec.fields[k] = float(v) if v is not None else None
                except Exception:
                    rec.fields[k] = None
        rec.raw_text = txt
        rec.ts = now_ts()

        # UI更新
        self._update_form_for_record(rec)
        self.status.set(f"GPT Parse done for {Path(rec.img_path).name}")

    # ---------- フォーム ----------
    def _clear_forms(self):
        for w in self.form_frame.winfo_children():
            w.destroy()
        self.form_by_uid.clear()

    def _refresh_forms_for_page(self):
        self._clear_forms()
        items = [
            r
            for r in self.records.values()
            if r.pdf_path == self.pdf_path and r.page_index == self.page_index
        ]
        items.sort(key=lambda r: (r.crop_rect[1], r.crop_rect[0]))
        for rec in items:
            self._append_form_for_record(rec, pending=(rec.uid in self.pending_jobs))

    def _append_form_for_record(self, rec: CropRecord, pending: bool = False):
        frame = ttk.LabelFrame(
            self.form_frame, text=Path(rec.img_path).name, style="Crop.TLabelframe"
        )
        frame.pack(fill=tk.X, padx=(0, 10), pady=(0, 10))
        frame.columnconfigure(2, weight=1)

        try:
            im = Image.open(path_for_display(rec.img_path))
            im.thumbnail((260, 260))
            ph = ImageTk.PhotoImage(im)
        except Exception:
            ph = None

        thumb = ttk.Label(frame, style="Thumb.TLabel")
        if ph:
            thumb.configure(image=ph)
            thumb.image = ph
        thumb.grid(row=0, column=0, rowspan=13, padx=(0, 14), pady=(2, 8))

        vars_map: dict[str, tk.Variable] = {}

        def add_field(row: int, key: str, label: str):
            ttk.Label(frame, text=label, style="Field.TLabel").grid(
                row=row, column=1, sticky="e", padx=(0, 8), pady=3
            )
            v = tk.StringVar(
                value="" if rec.fields.get(key) is None else str(rec.fields.get(key))
            )
            e = ttk.Entry(frame, textvariable=v, width=18)
            e.grid(row=row, column=2, sticky="ew", padx=(0, 4), pady=3)
            if pending:
                e.configure(state="disabled")
            vars_map[key] = v

        frame.vars_map = vars_map
        frame.uid = rec.uid

        add_field(0, "timestamp", "timestamp")
        add_field(1, "lat", "Latitude")
        add_field(2, "lon", "Longitude")
        add_field(3, "dep", "Depth")
        add_field(4, "len", "Length")
        add_field(5, "wid", "Width")
        add_field(6, "strike", "Strike")
        add_field(7, "dip", "Dip")
        add_field(8, "rake", "Rake")
        add_field(9, "slip", "Slip")
        add_field(10, "mw", "Mw")

        status_lbl = ttk.Label(
            frame,
            text=("Analysing..." if pending else "Ready"),
            foreground=(COLOR_WARN if pending else COLOR_ACCENT_DARK),
        )
        status_lbl.grid(row=11, column=1, sticky="w", padx=(0, 8), pady=(8, 0))

        def do_save():
            for k, v in vars_map.items():
                val = v.get().strip()
                if k == "timestamp":
                    rec.fields[k] = val
                else:
                    try:
                        rec.fields[k] = float(val) if val != "" else None
                    except Exception:
                        rec.fields[k] = None
            rec.ts = now_ts()
            # ここを置換：upsert_jsonl/CSV 呼び出しではなく全量保存
            persist_all(self.records, self.pdf_path)
            self.status.set(f"Saved {Path(rec.img_path).name}")

        btns = ttk.Frame(frame, style="Card.TFrame")
        btns.grid(row=11, column=2, sticky="e", pady=(8, 0))
        save_btn = ttk.Button(btns, text="Save", command=do_save, style="Accent.TButton")
        save_btn.pack(side=tk.LEFT)
        delete_btn = ttk.Button(
            btns,
            text="Delete",
            command=lambda: self._delete_record(rec),
            style="Danger.TButton",
        )
        delete_btn.pack(side=tk.LEFT, padx=(6, 0))

        frame.save_btn = save_btn
        frame.status_lbl = status_lbl
        frame.delete_btn = delete_btn

        if pending:
            delete_btn.configure(state="disabled")
            save_btn.configure(state="disabled")
            status_lbl.configure(foreground=COLOR_WARN)

        self.form_by_uid[rec.uid] = frame

    def _update_form_for_record(self, rec: CropRecord):
        frame = self.form_by_uid.get(rec.uid)
        if not frame:
            # まだ描かれていない場合はページ更新で再描画
            self._refresh_forms_for_page()
            return
        # 値反映
        for k, v in frame.vars_map.items():
            cur = rec.fields.get(k)
            v.set("" if cur is None else str(cur))
        # 有効化
        # Entry を有効化
        for child in frame.winfo_children():
            if isinstance(child, ttk.Entry):
                child.configure(state="normal")
        # ★ Saveボタンを有効化
        if hasattr(frame, "save_btn"):
            frame.save_btn.configure(state="normal")

        if hasattr(frame, "delete_btn"):
            frame.delete_btn.configure(state="normal")

        if hasattr(frame, "status_lbl"):
            frame.status_lbl.configure(text="Ready", foreground=COLOR_ACCENT_DARK)
        self.update_idletasks()

    def _delete_record(self, rec: CropRecord):
        if messagebox.askyesno(
            "Confirm", f"Delete record for {Path(rec.img_path).name}?"
        ):
            self.canvas.delete("current_rect")
            self.records.pop(rec.uid, None)
            persist_all(self.records, self.pdf_path)
            self._refresh_forms_for_page()
            self._draw_saved_rects_on_canvas()
            self.status.set(f"Deleted {Path(rec.img_path).name}")

    # ---------- 保存 ----------
    def save_all_on_page(self):
        # 画面にある/ないに関わらず「現在のself.records全体」を正として保存
        persist_all(self.records, self.pdf_path)
        cnt = sum(1 for rec in self.records.values() if rec.pdf_path == self.pdf_path)
        self.status.set(f"Saved {cnt} record(s)")


if __name__ == "__main__":
    app = App()
    app.mainloop()
