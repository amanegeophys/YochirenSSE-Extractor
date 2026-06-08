# <img src="assets/icon.svg" width="36" alt="YochirenSSE-Extractor icon"> YochirenSSE-Extractor

地震予知連絡会などのPDFから、スロースリップ関連の図を切り出し、OpenAI APIで数値を読み取り、CSV / JSONLとして保存するためのデスクトップアプリです。

PDFを開いて、図をドラッグするだけ。解析結果は右側のフォームで確認・修正して保存できます。

## できること

- PDFページを見ながら、必要な図だけをドラッグで切り出し
- GPTによる `timestamp`, `lat`, `lon`, `dep`, `strike`, `dip`, `rake`, `slip`, `mw` などの自動抽出
- 結果を `data/session/` にCSVとJSONLで保存
- 途中まで作業したPDFを開き直して、前回の切り出し結果を自動読み込み
- `app.py` と同じフォルダを基準に動くため、別のPCへフォルダごと移しても同じ構成で利用可能

## フォルダ構成

```text
YochirenSSE-Extractor/
├── app.py                  # アプリ本体
├── README.md               # この説明書
├── assets/
│   └── icon.svg            # README用アイコン
├── downloads/              # PDF置き場
├── data/session/           # 解析結果の保存先
├── data/sse_catalog.csv    # session CSVを結合した一覧
├── notebooks/
│   └── Yochiren_downloader.ipynb  # PDFダウンロード用
├── scripts/
│   └── build_sse_catalog.py       # sse_catalog.csv生成
├── .env.example            # APIキー設定の見本
└── pyproject.toml          # 依存ライブラリ設定
```

`downloads/` と `data/` はローカル作業用です。大きなPDFや解析結果、APIキーはGitに入れない設定にしています。

## はじめての使い方

### 1. 必要なもの

- Python 3.12以上
- OpenAI APIキー
- `uv` が使える環境

### 2. ライブラリを入れる

```bash
uv sync
```

### 3. APIキーを設定する

`.env.example` を参考に、同じフォルダに `.env` を作ります。

```text
OPENAI_API_KEY=あなたのAPIキー
```

`.env` は `.gitignore` に入っているため、他の人から見えない場所として扱えます。APIキーを `app.py` に直接書く必要はありません。

### 4. 起動する

```bash
uv run python app.py
```

## 操作方法

1. `File > Open PDF...` からPDFを開きます。
2. 左側のPDFビューで、読み取りたい図をドラッグして囲みます。
3. 右側に切り出し画像と入力フォームが追加されます。
4. 自動解析が終わったら、値を確認して必要なら修正します。
5. `Save` または `Save All on Page` で保存します。

## キー操作

| キー | 動き |
| --- | --- |
| `Left` | 前のページへ |
| `Right` | 次のページへ |
| `Enter` | 現在ページの内容を保存 |
| `Space` | 幅にフィット |
| `Ctrl + Mouse Wheel` | ズーム |

## 別のPCで作業を続ける

このフォルダを丸ごと別のPCへ移動し、同じ場所に `app.py`, `downloads/`, `data/` を置けば、既存の作業データを読み込めます。

保存ファイル名はPDF名を基準に作るため、PCごとの絶対パスが変わっても前回データを見つけやすくしています。切り出し画像も `data/session/crops/` から自動的に探します。

## SSEカタログを作る

`data/session/_*.csv` を結合して、時系列順の `data/sse_catalog.csv` を作るには次を実行します。

```bash
uv run python scripts/build_sse_catalog.py
```

このスクリプトは `timestamp` の `午前` / `午後` を `AM` / `PM` に正規化し、ファイル名から年を補完して並べ替えます。入力や出力を変えたい場合は、次のように指定できます。

```bash
uv run python scripts/build_sse_catalog.py --session-dir data/session --output data/sse_catalog.csv
```

## APIキーを守るために

- APIキーは `.env` にだけ書く
- `.env` を共有しない
- `app.py` にAPIキーを直接書かない
- もしAPIキーを一度でも公開してしまった場合は、OpenAIの管理画面でそのキーを無効化して作り直す

## メモ

ダウンロード用ノートブックは `notebooks/Yochiren_downloader.ipynb` に置いています。PDFを増やしたいときだけ使えば大丈夫です。`sse_catalog.csv` の生成は `scripts/build_sse_catalog.py` に分けています。
