# Meeting Minutes

動画または音声ファイルをドラッグ&ドロップして、ローカルで議事録を生成するアプリです。FFmpeg で 16kHz・mono WAV に変換し、WhisperX と Pyannote で日本語文字起こしと話者分離を行い、要約・決定事項・TODO を抽出します。

## 構成

- `app/server.py`: Python 標準ライブラリ製のローカル Web サーバー/API
- `static/`: ドラッグ&ドロップ UI、会議一覧、詳細画面、進捗表示
- `data/`: ローカル保存先。アップロードファイル、変換済み WAV、WhisperX 出力、議事録 JSON を保存

## 対応形式

- 動画: `mp4`, `mov`, `mkv`, `webm`
- 音声: `wav`, `mp3`, `m4a`

## 起動

初回のみ `.env.example` を `.env` にコピーし、`HF_TOKEN` を自分のトークンに置き換えます。`.env` は Git の公開対象から除外されます。起動時に自動で読み込まれ、ターミナルで設定した環境変数が優先されます。

```bash
cd ~/dev/meeting-minutes
cp .env.example .env
python3 app/server.py
```

ブラウザで `http://127.0.0.1:8888` を開きます。

ポートを変える場合:

```bash
PORT=8787 python3 app/server.py
```

## FFmpeg の準備

macOS では Homebrew でインストールします。

```bash
brew install ffmpeg
ffmpeg -version
```

アプリは動画・音声どちらの場合も次の形式へ変換します。

- 16kHz
- mono
- WAV
- `pcm_s16le`

## Python 環境

アプリ本体は Python 標準ライブラリのみで起動できます。ただし WhisperX は別途 Python 仮想環境へ入れてください。参考記事の通り、Mac では新しすぎる Python より `python@3.12` の利用が安定しやすいです。

```bash
brew install python@3.12
cd ~/dev/meeting-minutes
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install whisperx
```

仮想環境内で `whisperx --help` が動くことを確認してください。

```bash
source .venv/bin/activate
whisperx --help
python app/server.py
```

## Pyannote / Hugging Face の準備

話者分離には Pyannote のモデル利用許諾と Hugging Face トークンが必要です。

1. Hugging Face アカウントを作成します。
2. 次のモデルページで利用条件に同意します。
   - `pyannote/speaker-diarization-3.1`
   - `pyannote/segmentation-3.0`
   - `pyannote/speaker-diarization-community-1`
3. Hugging Face の Settings から Read 権限の Access Token を作成します。
4. `.env` の `HF_TOKEN` に設定します。

トークンを設定したらサーバーを再起動してください。`.env` は共有・コミットしないでください。

## WhisperX 実行設定

デフォルトでは次の設定で実行します。

```bash
whisperx audio_16k_mono.wav \
  --model large-v3 \
  --language ja \
  --device cpu \
  --diarize \
  --diarize_model pyannote/speaker-diarization-3.1 \
  --hf_token "$HF_TOKEN" \
  --output_format json
```

環境変数で変更できます。

- `WHISPERX_MODEL`: 例 `large-v3`, `medium`, `small`
- `WHISPERX_DEVICE`: Mac ではまず `cpu` 推奨
- `PYANNOTE_MODEL`: 既定値 `pyannote/speaker-diarization-3.1`

## AI 要約

`OPENAI_API_KEY` がある場合は OpenAI 互換の Chat Completions API で要約・決定事項・TODO を JSON 抽出します。未設定の場合も、アプリは簡易抽出で最後まで進みます。

`.env` の `OPENAI_API_KEY` と `OPENAI_MODEL` を設定してください。外部 API を使わない場合は `OPENAI_API_KEY` を空欄のままにします。

OpenAI 互換サーバーを使う場合:

`.env` の `OPENAI_BASE_URL` のコメントを外し、接続先と `OPENAI_MODEL` を設定します。

## 処理ステータス

UI には次の順番で進捗を表示します。

1. アップロード
2. 音声抽出
3. 文字起こし
4. 話者分離
5. 要約
6. 完了

エラーが起きた場合は詳細画面に原因を表示します。WhisperX の標準出力と標準エラーは各会議ディレクトリの `whisperx.stdout.log` と `whisperx.stderr.log` に保存します。

## ローカル保存

すべてのデータは `data/` に保存されます。

- `data/meetings.json`: 会議一覧と生成結果
- `data/meetings/<meeting-id>/`: 元ファイル、変換済み WAV、WhisperX 出力

クラウドへ自動送信しません。AI 要約に外部 API を使う場合のみ、文字起こしテキストが設定先 API に送信されます。

## 動作確認用モック

WhisperX 未インストールの状態で UI の完了フローだけ確認したい場合:

```bash
MEETING_MINUTES_MOCK=1 python3 app/server.py
```

FFmpeg 変換は実行されるため、短い音声または動画ファイルを使ってください。

## 公開・配布を考える場合

このリポジトリはローカル起動できるプロトタイプです。Mac App Store で公開する場合と、自社サイトで `.dmg` / `.pkg` 配布する場合では難易度が大きく変わります。

### まず現実的なルート

初期公開は Mac App Store よりも、Developer ID で署名・notarization した macOS アプリとして自社サイト配布する形が現実的です。理由は次の通りです。

- WhisperX / Pyannote / PyTorch / モデルファイルが大きく、App Store 向けの同梱・更新・審査説明が重い
- FFmpeg の同梱はライセンス確認が必要。GPL ビルドを同梱するとアプリ全体の配布条件に影響する可能性がある
- Mac App Store では App Sandbox 対応が必要で、外部バイナリ実行、ローカルファイルアクセス、モデル保存先、Hugging Face トークン管理を慎重に設計する必要がある
- 話者分離モデルは Hugging Face 側で利用許諾が必要なため、ユーザー自身のトークン入力フローが必要

### 公開用アプリ化の候補

1. Tauri で macOS デスクトップアプリ化
   - フロントエンドは現在の `static/` を流用
   - Python バックエンドは sidecar として起動、または Rust 側から処理を呼び出す
   - Electron より配布サイズを抑えやすい

2. Electron で macOS デスクトップアプリ化
   - Web UI をそのまま活かしやすい
   - Python / FFmpeg / WhisperX 周辺の同梱は別途設計が必要
   - 配布サイズは大きくなりやすい

3. Web アプリ + ローカルワーカー方式
   - UI はブラウザ、重い処理はユーザーのローカル CLI/デーモン
   - App Store より導入説明が難しくなるが、AI 依存の更新は楽

### Mac App Store を狙う場合の追加要件

- Apple Developer Program 登録
- Xcode プロジェクト化
- Bundle ID / App Icon / Copyright / プライバシー説明文の整備
- App Sandbox entitlement の設計
- ユーザーが選択したファイルだけを扱うファイルアクセス設計
- マイクを使うなら `NSMicrophoneUsageDescription`
- ネットワークを使う場合は API 送信内容を明示し、プライバシーポリシーを用意
- FFmpeg / WhisperX / Pyannote / PyTorch / モデルのライセンス確認
- 大容量モデルを同梱しない場合は初回セットアップ画面とダウンロード管理
- TestFlight または直接配布で十分に検証してから App Review へ提出

### おすすめの段階

1. まず現在のローカル版を完成させる
2. Tauri で `.app` 化する
3. Developer ID 署名・notarization 済み `.dmg` を作る
4. 利用者の環境構築負担を減らすインストーラーを作る
5. ライセンス・sandbox・モデル配布方針が固まったら Mac App Store 対応を検討する

## 参考

- [Macローカル環境 WhisperX + Pyannote 構築ガイド](https://note.com/yonmas/n/n2854d2c1c75f)
