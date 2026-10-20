# ARK Server + Discord Bot

DiscordからARKサーバーの状態確認・起動・確認付き再起動を行う、rootless Docker構成です。ゲームとBotを別コンテナで動かし、保存・終了の検証とセーブデータの退避を行います。

対象はSteam版 **ARK: Survival Evolved（ASE）** です。ARK: Survival Ascended（ASA）には対応していません。

## 特徴

- **Discordから操作** — `/ark status`・`/ark start`・`/ark restart`
- **無人時の自動停止** — 約1時間ごとに人数を確認し、条件を満たす場合に保存・停止
- **ゲームとBotを分離** — BotにDockerソケット・セーブデータ・ゲームのパスワードを渡さない
- **rootless Docker** — 通常ユーザーで動作し、コンテナの追加権限や管理用TCPポートは不要
- **起動と更新を分離** — 起動時の自動更新や、未知のクラッシュ後の自動再起動は行わない

## 導入方法

### 動作環境

| 項目 | 要件 |
|---|---|
| ホスト | Linux x86_64、systemd、cgroup v2、Landlock ABI 3以上 |
| Docker | 専用のrootless Dockerエンジン |
| Python | ホスト側ツールに使用。3.12／3.14でテスト |
| ゲーム | ASEのLinuxサーバー。停止検証は既知の固定ビルド向け |
| Discord | Botを導入できるDiscordサーバー1つ |

### 初回セットアップ

```bash
git clone https://github.com/tanabe1478/ark-server.git
cd ark-server
```

1. [Docker構築手順](docs/docker.md)に沿って、rootless Docker・ゲーム本体・MOD・認証設定を準備します。
2. [Discord設定](docs/discord.md)に沿って、Botを作成し、トークンと通知先をローカルに設定します。
3. イメージのビルド、別データコピーでの起動・保存・停止検証、初回切替を[Docker構築手順](docs/docker.md)に沿って行います。

rootless Dockerの準備後、イメージだけをビルドする場合は次を使います。**この操作ではゲームは起動しません。**

```bash
python3 -m scripts.docker_setup build-runtime
python3 -m scripts.docker_setup build-bot
```

> 配布物はソースです。ゲーム本体・MOD・設定済みイメージは含まず、`docker compose up`だけで起動する構成ではありません。導入手順は既存Native構成からの切替が中心で、新規PCの完全自動構築には対応していません。

## 実行方法

以下は初回セットアップと検証が完了した環境で使用します。

### Discordから操作

| コマンド | 動作 |
|---|---|
| `/ark status` | 人数・参加可能性・稼働時間・ゲームとPC全体のメモリを表示 |
| `/ark start` | 設定と起動状態を確認して起動。起動済みなら二重起動しない |
| `/ark restart` | 人数を表示し、実行者本人の期限付き確認を受けて再起動 |

登録したDiscordサーバーの全メンバーが利用できます。DM・他のDiscordサーバーからの操作は拒否します。Discord側のチャンネル・コマンド権限は別途適用されます。

### ホストから操作

| 操作 | コマンド |
|---|---|
| 状態確認 | `python3 scripts/arkctl.py status` |
| 起動 | `python3 scripts/arkctl.py start` |
| 停止 | `python3 scripts/arkctl.py stop --confirm` |
| 再起動 | `python3 scripts/arkctl.py restart --confirm` |

通常操作はDiscordまたはこのCLIを使います。直接の`docker restart`や`systemctl restart`は、保存・終了検証を伴う操作の代わりにはなりません。

### 自動停止

約1時間ごとに無人かどうかを確認します。**「連続して1時間無人だったら停止」ではありません。** 起動・start依頼後には猶予があり、人数を取得できない場合は停止しません。

ARKが停止しても、PC・Dockerエンジン・Bot・ホストの制御サービスは動かしておきます。PCの電源投入やスリープ解除は行いません。

## 設定

設定は環境変数ではなく、ローカルのファイルで管理します。

| ファイル・ディレクトリ | 内容 |
|---|---|
| `config/server.json` | マップ、サーバー名、最大人数、ゲーム／Queryポート |
| `server/ShooterGame/Saved/Config/LinuxServer/` | ゲームのINI。参加・管理者パスワード、倍率、MOD設定など |
| `config/discord-bot.json` | 対象Discordサーバー、通知先、安全スイッチ |
| `config/discord-bot.token` | Botトークン。所有者だけが読めるファイルに保存 |

初期設定では、**再起動と自動停止は無効**です。環境ごとの検証が完了してから有効化します。設定変更にも停止と再検証が必要です。詳細は[通常運転](docs/control.md)・[Discord設定](docs/discord.md)を参照してください。

これらの実設定・認証情報はGitHubやイメージへ含めません。[公開・秘密情報の取り扱い](SECURITY.md)

## ポート設定

| 標準ポート | プロトコル | 用途 |
|---|---|---|
| 7777・7778 | UDP | ゲーム通信 |
| 27015 | UDP | A2S／Steam問い合わせ |

ゲームポートを変更した場合は隣接ポートも必要です。RCONや管理用HTTP/TCPポートは公開しません。Botは外向きのDiscord Gateway／HTTPS通信を使います。[通信設定の詳細](NETWORK.md)

## データとバックアップ

| ホスト側のディレクトリ | 内容 |
|---|---|
| `server/` | ゲーム本体・MOD。コンテナからは読み取り専用 |
| `server/ShooterGame/Saved/` | セーブデータとゲーム設定。この領域は書き込み可能 |
| `backups/` | 保存・停止検証時に退避した、停止前後のSavedなど |
| `logs/` | 操作・監査・保存／終了検証の記録 |

これらはローカル専用です。実データをリポジトリや公開イメージへ同梱せず、ログ・バックアップも公開しないでください。古いバックアップの自動削除は行いません。

## サーバーの更新

**ゲーム・MOD・イメージを起動時に自動更新しません。** イメージはレビュー済みのIDに固定し、更新候補は本番と分離した環境で検証します。

稼働中のソースやゲーム資産を上書きせず、現行環境での停止・退避と、候補環境での検証・承認を分けて行います。[更新・復旧の手順](docs/update-recovery.md)

## 停止検証について

対応する固定ASEビルドでは、保存後にもSIGABRTで終了する経路を観測しています。**終了値6／134だけを正常扱いすることはありません。** 保存・終了の証拠がそろった場合に限り、正常終了とは区別した警告付き停止として扱います。

未知のクラッシュ、OOM、証拠不足では自動で再起動しません。また、保存完了の観測はゲーム内の内容やMOD互換性を保証するものではありません。[停止検証の設計と制約](docs/evidence-qualified-shutdown.md)

## 関連ドキュメント

- [Docker構築・初回切替](docs/docker.md)
- [Discord Botの設定](docs/discord.md)
- [通常運転・監査・失敗時の対応](docs/control.md)
- [更新・復旧](docs/update-recovery.md)／[データ移行](docs/migration.md)
- [通信設定](NETWORK.md)／[秘密情報の取り扱い](SECURITY.md)
- [実ゲームによる検証チェックリスト](docs/live-validation.md)

<details>
<summary>開発・テスト</summary>

```bash
python3 -m venv .venv
.venv/bin/pip install --only-binary=:all: --require-hashes -r requirements-discord.lock
.venv/bin/pip install --only-binary=:all: -r deploy/requirements.txt
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
python3 -m scripts.public_export
python3 -m scripts.public_export --history
```

通常のテストは偽バックエンド・合成データを使用し、実ゲーム・systemd・Discord認証には接続しません。必要な依存を入れない場合、対応する試験はスキップされます。

</details>

## ライセンス

[MIT License](LICENSE)。ARK・Steamのゲーム本体やMODは含みません。商標や依存ソフトウェアの権利・ライセンスは、それぞれの権利者に帰属します。
