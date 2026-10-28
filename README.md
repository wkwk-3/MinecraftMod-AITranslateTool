# MC MOD Translator

Minecraft の Mod を日本語化するツールです。Mod の言語ファイル (en_us) をローカル LLM で翻訳し、日本語化リソースパックを作ります。FTB Quests のクエストも翻訳できます。

Google Colab の GPU で動かすことを想定しています。手元の PC でも動きます。

## できること

- 複数の Mod をまとめて翻訳し、1 つのリソースパック (ZIP) に書き出す
- 前提 Mod を見つけて一緒に翻訳する
- ガイドブックやマニュアルなど、lang 以外のファイルも翻訳する
- FTB Quests のクエストを翻訳する
- 翻訳が途中で止まっても、続きから再開する

## Colab で使う

### 初回の準備

1. このフォルダを GitHub に置くか、ZIP にまとめます。ZIP の場合は名前を `mc-mod-translator.zip` にし、`run.py` が ZIP の一番上に来るようにします。
2. ngrok を使う場合は、ngrok の authtoken を Colab のシークレットに `NGROK_AUTHTOKEN` という名前で登録します。

### 毎回の手順

1. `MC_Mod_Translator.ipynb` を Colab で開き、ランタイムのタイプを T4 GPU にします。
2. ZIP を使う場合は、Colab の「ファイル」欄で `/content/` にアップロードします。
3. セル 1 で設定を入力し、セル 1 から 4 を順に実行します。
4. セル 4 に表示された URL を開きます。

セル 2 では Google ドライブへの接続を求められます。llama.cpp のビルドに 10〜15 分かかります。`SAVE_LLAMA_TO_DRIVE` をオンにすると、2 回目からはドライブに保存したものを使うのでビルドを省けます。セル 4 は実行したままにしてください。止めると画面が使えなくなります。

### セル 1 の設定

| 項目 | 内容 |
|---|---|
| `REPO_URL` | GitHub のリポジトリ URL。ZIP を使う場合は変更不要 |
| `TUNNEL` | 画面への接続方法。下の表を参照 |
| `DRIVE_DIR` | Google ドライブ上の保存先。既定は `MyDrive/mc-mod-translator` |
| `AUTO_RESUME` | 中断した翻訳を起動時に再開する |
| `SAVE_LLAMA_TO_DRIVE` | llama.cpp (llama-server) をドライブに保存し、次回からそこから読み込む。既定はオフ (毎回ビルドする) |
| `LLAMA_DRIVE_DIR` | llama-server の保存先・読み込み元。既定は `MyDrive/llama`。`SAVE_LLAMA_TO_DRIVE` がオンのときだけ使う |

`LLAMA_DRIVE_DIR` に llama.cpp 公式の Linux CUDA 版 (`llama-bXXXX-bin-ubuntu-cuda-12.8-x64.tar.gz` と `cudart-llama-bXXXX-bin-ubuntu-cuda-12.8-x64.tar.gz`) を置くと、展開してそれを使います。Colab の環境に合わずに起動できない場合は、自動でビルドに切り替えます。tar.gz は展開せずにそのままドライブに置いてください。

| 接続方法 | 必要なもの | 備考 |
|---|---|---|
| ngrok | `NGROK_AUTHTOKEN` | 既定 |
| cloudflared | なし | アカウント登録が不要 |
| colab | なし | ノートブックを開いている本人だけが接続できる |

どの方法でも、起動時に作られるトークンが URL に含まれていないと画面を開けません。この URL は他の人に教えないでください。

## 手元の PC で使う

```bash
pip install -r requirements.txt
bash setup_llama.sh ./llama.cpp
python run.py --workdir D:/mct-work
```

表示された URL をブラウザで開きます。llama.cpp をビルドしない場合は、環境変数 `LLAMA_SERVER` に llama-server のパスを指定してください。Ollama や LM Studio を使う場合は、画面の「モデル」欄の詳細で OpenAI 互換サーバーの URL を指定します。

| オプション | 内容 |
|---|---|
| `--workdir` | 保存先のフォルダ。省略すると `./mct_work` |
| `--resume latest` | 中断した翻訳を起動時に再開する |
| `--tunnel` | `ngrok` / `cloudflared` / `colab`。省略するとこの PC からのみ接続できる |

## 画面

### 翻訳タブ

1. Mod の JAR をアップロードし、翻訳する Mod を選びます。Shift+クリックで範囲選択、Ctrl+クリックで追加選択ができます。
2. 必要に応じて用語集や翻訳の指示を入力します。
3. 「翻訳する」を押します。終わると ZIP をダウンロードできます。

できあがった ZIP は、Minecraft の resourcepacks フォルダに入れて有効にしてください。

### 追加リソースタブ

Patchouli のガイドブック、マニュアル、説明テキストなど、lang 以外で翻訳できるファイルを探します。翻訳が必要か、リソースパックで差し替えられるかは補助モデルが判定します。翻訳するファイルを選んで「翻訳する」を押すと、lang と同じリソースパックにまとめて書き出します。

`data/` 以下のファイルはリソースパックでは変更できないため、選択できません。

### FTB Quests タブ

1. インスタンスの `config/ftbquests` フォルダか、その ZIP を選びます。
2. アイテム名の辞書に使うリソースパックを選びます。
3. 「翻訳する」を押します。

書き出された ZIP には `config/ftbquests/quests/lang/ja_jp…` が入っています。インスタンスのフォルダに展開してください。既存の `ja_jp` フォルダと `ja_jp.snbt` は、展開する前にバックアップしてください。

クエスト内のアイテム名は、Mod の英語名とリソースパックの日本語名を対応付けた辞書を使って統一します。辞書を作るには、クエストに出てくる Mod の JAR を翻訳タブでアップロードしておく必要があります。

## モデル

翻訳には Index-Translate だけを使います。Index-Translate の公式の依頼形式 (instTrans) で翻訳を依頼します。

| 翻訳モデル | 大きさ (Q4_K_M) | 目安 |
|---|---|---|
| IndexTeam/Index-Translate-35B-A3B-preview-GGUF | 約 20GB | 既定。Colab の T4 では一部を CPU で処理する |
| IndexTeam/Index-Translate-9B-GGUF | 約 5.4GB | GPU のメモリが 8GB 以上 |
| IndexTeam/Index-Translate-2B-GGUF | 約 1.3GB | GPU のメモリが少ない場合 |

量子化は GPU のメモリに合わせて自動で選びます。Hugging Face の GGUF ファイルの URL を「モデル」欄に入力すると、ダウンロードして候補に追加します (翻訳モデルは Index-Translate のみ)。Q2_K などの小さい量子化は、訳の質が大きく下がります。

補助モデルは既定では使いません。用語集の取り込みと追加リソースの判定はルールで行います。補助モデルを選ぶと、前提 Mod の照合、文中の用語の抽出、追加リソースの判定にも使います。

### Windows で動かす場合

llama.cpp の Windows 用 CUDA 版 (`llama-bXXXX-bin-win-cuda-12.4-x64.zip` と `cudart-llama-bin-win-cuda-12.4-x64.zip`) を https://github.com/ggml-org/llama.cpp/releases から同じフォルダに展開し、環境変数 `LLAMA_SERVER` に `llama-server.exe` のパスを指定してください。

## 用語集

1 行に 1 語を書きます。

```
原文 = 訳語 | メモ | 区別
```

メモは翻訳時に AI へ渡す補足です。「区別」を付けると大文字と小文字を区別して照合します。

用語集は次の順に優先されます。

1. 自分で入力した用語
2. Mod に同梱されている日本語訳から取り込んだ用語
3. 翻訳中に自動で登録した用語
4. Minecraft の基本用語

## 翻訳の品質

- 同じ Mod の項目はまとめて翻訳し、直前の訳を文脈として渡します。
- 一度訳した文は翻訳メモリに保存し、同じ文には同じ訳を使います。
- `%s`、`{name}`、`§a`、改行などは記号に置き換えてから翻訳し、訳文で元に戻します。戻せない場合は翻訳し直し、それでも失敗した項目は英語のまま残します。
- Mod の文章に命令文が含まれていても、AI はそれに従わずに翻訳します。

## 保存先と再開

Mod、翻訳結果、翻訳の途中経過、実行履歴は保存先のフォルダに保存します。Colab では Google ドライブ、手元の PC では `--workdir` で指定したフォルダです。

```
<保存先>/
  mods/             アップロードした Mod
  output/           書き出した ZIP
  jobs/             翻訳の途中経過
  history.jsonl     実行履歴
  memory.json       翻訳メモリと用語集
  resources.json    追加リソースの検出結果
  settings.json     モデルと翻訳の指示の設定
  ftbquests/        FTB Quests の取り込みデータ
```

途中経過は少しずつ保存しているため、Colab の接続が切れても失われません。起動し直すと中断した翻訳が表示され、「再開」を押すと続きから翻訳します。

## 注意

- 無料版の Colab は 1 回の利用時間が最大で約 12 時間です。GPU の利用枠にも上限があります。
- 35B のモデルは T4 の GPU メモリに収まりきらないため、一部を CPU で処理します。読み込めない場合は、小さい量子化を選ぶか、別のモデルを使ってください。
- アップロードした Mod や翻訳結果は自分の Google ドライブに保存されます。保存先のフォルダを共有しなければ、他の人からは見えません。

## テスト

```bash
python -m pytest -q
```
