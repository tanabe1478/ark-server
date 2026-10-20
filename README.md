# ARK Linux server + Discord Bot

Steam版 **ARK: Survival Evolved（ASE、ASAではありません）** の保存を保護しながら、Discordから起動・状態確認・確認付き再起動を行う構成です。Linux x86_64、rootless Docker、systemd user serviceを使用します。

**コードと実データは別物です。** このリポジトリ／ビルドコンテキストに、ゲーム本体、Saved、MOD本体、パスワード、Botトークン、Discord ID、ログ、バックアップを入れないでください。公開前は [SECURITY.md](SECURITY.md) を確認してください。

## できること

指定した一つのDiscord guildの全メンバーが利用できます。DM・他guildは拒否し、個人／ロールの許可リストは使いません。Discord側のチャンネル・コマンド権限は別途適用されます。

- `/ark status` — 人数、参加可能性、稼働時間、ゲームとPC全体のメモリを表示
- `/ark start` — 二重起動を防ぎ、設定・A2S・観測器の準備を確認して通知
- `/ark restart` — 本人の期限付き確認、人数の再確認、保存・終了検証を経て再起動
- 約1時間ごとの無人判定 — 起動／依頼後の猶予と直前の二重照会。人数不明は0人にしない
- 再送・同時実行の抑止、監査、失敗／中断時の自動操作遮断

「1時間」は判定間隔であり、連続した無人時間ではありません。参加前のstart依頼は、起動済みでも猶予を更新します。PCの電源投入・スリープ解除は行いません。

## 構成

```text
Discord Gateway / HTTPS（外向き）
          |
   Botコンテナ                 ゲームコンテナ
   config/tokenだけ            固定ビルド・MODはread-only
   Docker socketなし           Savedだけを明示的に書込許可
          |                           ^
   owner-only Unix socket             |
          |                           |
   ホストの共通コントローラー ------ rootless Docker
   通常ユーザー / ロック / 証拠 / 退避
```

管理用HTTP/TCPポート、RCON、sudo、Dockerグループ権限をBotへ与えません。公開する通信は必要なゲームUDPだけです。Dockerデーモン自体も専用のrootless user serviceで動作し、既存のrootfulデーモンとは分離します。

コンテナ内UID 0はrootless namespace内の値で、ホストでは通常ユーザーに対応します。`--privileged`、追加capability、host PID namespace、seccomp無効化は使用しません。

## 導入と通常運転

1. [Docker構築・初回切替](docs/docker.md)
2. [Discordアプリ・秘密ファイルの設定](docs/discord.md)
3. [通常運転・監査・失敗時の対応](docs/control.md)
4. [更新と復旧](docs/update-recovery.md)
5. [通信設定](NETWORK.md)

初期値では **再起動と自動停止は無効** です。別のPC・イメージ・ゲームビルドは別の検証対象です。サンプルをコピーしただけで安全スイッチを有効化しないでください。

ゲームは起動時に更新しません。イメージはIDで固定し、ゲーム／MODの更新は新しい候補ディレクトリで実施します。実データをCIへ渡さず、公開イメージへValveのゲーム資産を組み込みません。

## 停止に関する重要事項

このASEビルドでは、要求に応じた保存後にもゲーム自身がSIGABRTで終了する経路を観測しています。**終了値6／134を無条件に正常扱いしません。**

固定されたバイナリ・観測コード・実行インスタンス、停止意図、ネイティブ保存完了、既知abort経路、実際の終了、設定維持、Savedの停止前後退避がそろう場合だけ **警告付き停止（`clean_exit=false`）** として次の起動を許可します。

Dockerでは、実ゲームのsignal 6に対しTini／コンテナ・ホストラッパーが134を返します。それぞれの結果を保全し、正常終了0へ変換しません。未知のクラッシュ、OOM、タイムアウト、証拠不足なら自動で起動し直しません。[設計と制約](docs/evidence-qualified-shutdown.md)

対応する観測profileはコード内の固定ビルド専用です。未知のビルドへアドレスを流用しないでください。保存関数の完了を観測しても、建築物・恐竜・アイテム・MODの完全なゲーム内整合性までは保証できません。

## 開発・公開前検査

```bash
python3 -m venv .venv
.venv/bin/pip install --only-binary=:all: --require-hashes -r requirements-discord.lock
.venv/bin/pip install --only-binary=:all: -r deploy/requirements.txt
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
python3 -m scripts.public_export
# 元リポジトリを公開する前には、過去のblobも確認する
python3 -m scripts.public_export --history
# 新規ディレクトリへ、履歴・実データを含まない公開候補を作る
python3 -m scripts.public_export --destination dist/public-source
```

通常のテストは偽バックエンド／合成データのみで、systemd・実ゲーム・Discord認証へ接続しません。SDKのない環境ではSDK固有試験をスキップします。実ゲームによる隔離試験は別作業で、[検証チェックリスト](docs/live-validation.md)を使用します。

エクスポーターは既存履歴を改変せず、Git初期化・commit・pushも行いません。公開対象と履歴の最終レビューは公開前に行ってください。

## ライセンス

このプロジェクトのソースとドキュメントは[MIT License](LICENSE)です。ARK・Steamのゲーム本体、MOD、保存データは含みません。これらの権利・商標、および個別の依存ソフトウェアのライセンスは、それぞれの権利者に帰属します。
