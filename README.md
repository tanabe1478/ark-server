# ARK private server

ARK: Survival Evolved 専用サーバー、Discord Bot、保存終了を検証するコントローラーをまとめた **rootless Docker Compose** 構成です。

初回の秘密ファイル作成後は、通常 **`docker compose up -d`** で起動できます。ゲーム・設定済みMOD・本リポジトリの実行コード更新は自動検出し、独立した Saved コピーで検証してから条件付きで適用します。

> ASE 用です。ARK: Survival Ascended 用ではありません。ゲーム本体・MOD・セーブ・実際の認証情報は配布しません。

## 特徴

- **Discord**：指定した1つのサーバーで `/ark status`・`/ark start`・確認付き `/ark restart`。メンバー全員が利用可能で、DM・別guildは拒否します。
- **無人時の省リソース化**：約1時間ごとの人数確認でゲームだけを停止。PC・Docker engine・コントローラー・Bot・platform証人は停止しません。
- **保護付き自動更新**：SteamCMD／Workshop／公式mainの変更を検出。プレイヤー・起動猶予・保存終了・独立コピーでの2回の保存／終了／再読込を確認します。
- **停止状態を保持**：意図的に止めた本番ゲームを更新のために起動しません。検証コピーの Saved を本番へ戻しません。
- **権限分離**：ゲームとBotに Docker socket・ホストsystemd busを渡しません。管理IPCは専用Unix socketで、HTTP管理ポートはありません。
- **障害時は保留**：不明なクラッシュ、OOM、観測喪失、適用中断、応答不明のcommitを自動でやり直しません。バックアップや失敗記録を自動削除しません。

## セットアップ

### 必要なもの

- Linux x86-64、cgroup v2、Landlock ABI 3以上
- **rootless Docker** と Docker Compose。検証対象は Docker 29系／Compose 2.40系です。
- 初回ファイル準備用 Python 3.12以上
- ゲームに加え、更新検証用の余裕があるメモリーとディスク
  - 設定例は検証前の `MemAvailable` を **12 GiB以上**要求します。32 GiB級のホストを推奨します。
  - ゲームの別コピー・複数世代の Saved・旧assetsを保持します。数百GiBの空き容量を見込み、使用量を監視してください。
- Discord Application／Bot、対象guildへの招待、返信・通知先への送信権限
- Steam・Workshop・GitHub・Discordへの外向き通信

Dockerの初期導入は [公式rootless手順](https://docs.docker.com/engine/security/rootless/) を参照してください。rootful socketを使えるようにするためのDockerグループ追加、`chmod 666`、`privileged` は不要・非対応です。

### 1. ソースとrootless接続先を準備

```bash
git clone https://github.com/tanabe1478/ark-server.git
cd ark-server
docker info --format '{{json .SecurityOptions}}'
```

選択済みのDocker接続先に `name=rootless` と標準seccompがあることを確認します。Composeとupdaterの接続先は**同じ専用engine**にしてください。

```bash
export ARK_DOCKER_SOCKET="$XDG_RUNTIME_DIR/docker.sock"
```

既存の専用socketを使う場合は、そのパスを指定します。`DOCKER_HOST`／contextの選択は初期準備側で行い、このプロジェクトが既定contextやホストのrootful daemonを変更することはありません。

### 2. 秘密ファイルを作成

```bash
python3 -m scripts.compose_init
${EDITOR:-nano} config/compose/bootstrap.json
python3 -m scripts.compose_init --check
```

最初のコマンドは **Bot tokenを非表示入力**し、設定テンプレートを作ります。Discordへの接続・コマンド登録・投稿はしません。

`bootstrap.json` の置換箇所を編集してください。

| 項目 | 内容 |
|---|---|
| `password` / `admin_password` | 異なる参加用／管理者用パスワード。12～128文字の英数字・`_`・`-` |
| `discord.guild_id` | Botを使う1つのguild ID |
| `discord.notification_channel_id` | 自動停止通知先のchannel ID |
| `settings` | マップ、表示名、人数上限。例のマップは `TheIsland` |
| `active_mods` | Workshop ID文字列の配列。並び順を維持、最大32個 |
| `multipliers` | テイム・採取・経験値・昼夜・交配・孵化・成長倍率、刷り込みの間隔と量 |
| `auto_start` | 初回のゲーム自動起動。以後は管理操作で記録した稼働希望状態を保持 |
| `auto_stop` | 約1時間ごとの無人確認による停止 |
| `updates` | ゲーム・設定済みMOD・実行コード更新、確認間隔、検証用メモリー下限 |

秘密ファイルは `config/compose/` にowner-onlyで保存され、Git／ビルドcontext／公開exportには含まれません。tokenやパスワードをコマンド引数・環境変数・`.env`・チャットへ貼り付けないでください。

Botは `bot` と `applications.commands` のscopeで対象guildへ招待します。特権intentsやメンバー／roleのallowlistは使いません。

### 3. 起動

```bash
docker compose config --quiet
docker compose up -d
docker compose logs -f server
```

初回はソースのみのイメージをビルドし、公式SteamCMDからゲームと指定MODを取得します。最初のゲーム起動では、まず外部から参加できないloopback起動で実際の保存終了を検証し、その後通常起動します。ダウンロード・マップ読込・保存コピーには時間がかかります。`server` コンテナのhealthyは管理受信器の利用可否で、ゲームの準備完了と同じではありません。`manager status`／`/ark status` のphaseを確認してください。

`auto_start: false` ならゲームは起動せず、コントローラーとBotだけが利用可能になります。最初の `/ark start` 時に非公開の保存終了検証を行います。

### 4. ホスト再起動後も自動で戻す（推奨）

Composeのserviceはホスト再起動後に自動では戻りません。受入確認が済んだら、`.env` でproject名とsocketを固定し、`deploy/docker/ark-compose.service.in` からsystemd user unitを配置します。手順は[ホスト起動時の自動起動](docs/compose.md#ホスト起動時の自動起動)を参照してください。

既存データは通常起動で上書き初期化しません。[明示import手順](docs/compose.md#既存データのimport)を使ってください。importの既存認証は元INIとの一致を確認して保持し、新規用の長さ制約を理由にパスワードを黙って変更しません。

## 日常の操作

### Discord

| コマンド | 動作 |
|---|---|
| `/ark status` | 稼働、準備状態、人数、保存終了の警告を表示 |
| `/ark start` | 停止中なら起動。稼働済みなら再起動せず猶予を更新 |
| `/ark restart` | 要求者に紐づく60秒の確認後、人数を再確認して保存終了→起動 |

再起動と自動停止は実際の保存終了確認後に有効になります。Botは外向きGateway／HTTPSのみを使用します。作業報告のための任意投稿機能はありません。

自動停止は**約1時間間隔のサンプリング**です。「1時間連続で無人だった」証明ではありません。起動後・最近の起動要求からの3600秒の猶予、複数回の人数確認、保存前後の再確認を行います。不明／不正／取得不能な人数を0として扱いません。

### ホストの管理者

```bash
docker compose run --no-deps manager status
docker compose run --no-deps manager start
docker compose run --no-deps manager stop
docker compose run --no-deps manager restart
```

これらは専用Unix IPC経由のオーナー操作です。`stop` はゲームだけを保存終了し、稼働希望状態を「停止」にします。Botとコントローラーは残ります。

構成全体を終了する場合：

```bash
docker compose down
```

通常の終了猶予は25分です。ゲームの保存終了と受理済み処理の記録を待ちます。`kill`、極端に短いtimeout、`down -v`、volume／imageの一括pruneを通常運用に使わないでください。

> **稼働中の構成と同じディレクトリ・project名・Docker接続先で操作してください。** volume名はproject名で決まります。別のcheckoutや別のproject名で `docker compose up -d` すると、既存のワールドを使わない空のサーバーを新しく作って起動します。

### PCを再起動したとき

[自動起動のunit](docs/compose.md#ホスト起動時の自動起動)を設定済みなら、user managerの起動時にBot・コントローラーが戻ります。

1. 運用ユーザーでログインする（lingerを有効にしていない場合は必須）。ログアウトでも止まります。画面ロック・モニターの電源オフは影響しません。
2. `systemctl --user status ark-compose.service` でunitが成功していることを確認する。
3. `docker compose run --rm --no-deps manager status` または `/ark status` で状態を確認する。
4. 無人停止・`manager stop` の後に再起動した場合、ゲームは停止したままです。遊ぶときは `/ark start` で起動します。

`phase: stopped_with_warning` と `last_shutdown_warning: expected_native_abort` は、保存終了を確認した既知のSIGABRT停止です。通常の停止状態なので、記録の削除や `reset-failed` で消さないでください。PCの自動サスペンドはBotも止めるため、無効のままにしてください。

本番固有のパス・project名・接続先は、Git対象外の `*.local.md`（例：`OPERATIONS.local.md`）に手元で記録しておくと、別の担当者やセッションに引き継げます。

## 自動更新

デフォルトの確認間隔は3600秒です。

1. Steamのゲームbuild、設定済みMODの更新時刻、公式 `tanabe1478/ark-server` のmainを確認。
2. 本番 Saved・token・Docker socketのない別コンテナで取得／構築し、候補assetsとイメージを固定。
3. 対応するネイティブ保存終了命令構造を事前照合。非対応なら稼働中の旧版をそのまま維持。
4. 稼働中なら人数・猶予を確認して旧版で保存終了。最新 Saved を独立コピー。
5. 候補で2回の実際の準備完了→保存終了→再読込を検証。
6. 旧assets・設定・inspect・ログを保存して差し替え。Saved本体は巻き戻さず、元から稼働中だった場合だけ再開。

「イメージ更新」はレジストリのmutable tagを無条件pullする処理ではなく、取得した**正確な公開commitからのローカルbuild**です。READMEだけの変更では再buildしません。配線・契約schemaを破壊する更新や未知のネイティブ配置を無理に通しません。

```bash
docker compose exec updater python3 -m scripts.compose_update status
docker compose exec updater python3 -m scripts.compose_update check
```

空き容量不足・メモリー不足・プレイヤー参加・猶予中は保留します。不明な適用結果や起動結果は自動再送しません。[更新・中断復旧の詳細](docs/compose.md#更新と復旧)を参照してください。

## ポート

| ホスト既定値 | 用途 |
|---|---|
| `7777/udp` | ゲーム |
| `7778/udp` | ゲーム追加ポート |
| `27015/udp` | A2S query |

公開するのはこの3つのUDPだけです。RCONは無効、管理用TCP／HTTPポートはありません。ホスト側の変更は `ARK_BIND_ADDRESS`・`ARK_GAME_PORT`・`ARK_GAME_PORT_PLUS_ONE`・`ARK_QUERY_PORT` で行います。ゲームポートを変えるときは追加ポートもセットしてください。コンテナ内部の7777／27015は変更しません。

ファイアウォールやルーター設定を自動変更しません。外部からの参加・認証は使用環境で確認が必要です。

## データ・バックアップ・設定変更

| named volume | 内容 |
|---|---|
| `platform` | 更新中も交換しない安定証人のgeneration／終了状態。server／updaterはRO |
| `game` | ゲーム／MOD assets。通常のゲームには読み取り専用 |
| `saved` | ワールド、キャラクター、トライブ、INI。**ここが現在の進行データ** |
| `settings` | 構成、固定runtime契約、更新／初期化の許可記録 |
| `evidence` | 監査、実際の子プロセス終了、ネイティブ観測、更新台帳 |
| `backups` | 保存前後のコピー、旧assets、設定、中断時の保全 |
| `steam` / `staging` | SteamCMD cache、取得／検証候補 |

実際のvolume名にはCompose project名が付きます。project名を不用意に変えると別データを作るので、運用中は固定してください。

保存コピーには秘密情報が含まれます。外部へ公開しないでください。バックアップ・失敗した候補・旧イメージは自動削除しません。**古い Saved の復元で新しい進行を上書きする自動rollbackはありません。** 別媒体への保全と復元リハーサルも必要です。

起動後に `bootstrap.json` を編集しただけでは適用しません。[明示的な停止保守](docs/compose.md#設定変更)を利用します。未知のMOD追加やマップ変更を、通常のバージョン更新と同一視しません。

## 保存終了の扱いと限界

対象ASEでは保存終了後に既知経路の **SIGABRT（6）** が発生します。本実装はこれを終了0に書き換えません。

- `clean_exit=false`、警告は `expected_native_abort`。
- 起動／停止nonce、実行イメージ、バイナリー、設定、PID／tracer、保存開始→保存完了→既知abort、実際のworker wait結果、Savedの前後コピーを照合。
- Composeの永続コントローラーが生きている事実と、ゲーム子プロセスの終了を区別。
- OOM・timeout・別signal・不完全な証拠を一律に134として承認しない。
- readiness timeoutだけで稼働中のゲームをkill／再起動しない。

ハッシュやA2S、保存／再読込の自動試験だけでは、建築物・恐竜・アイテム・MOD互換性の完全性は分かりません。`in_game_content_verified=false` を維持し、実際のゲーム内確認を別に行ってください。

## 関連ドキュメント

- [Composeの構成・import・設定・更新復旧](docs/compose.md)
- [本番切替の受入確認：Discord・外部参加・ゲーム内／MOD](docs/acceptance.md)
- [Discordの権限とコマンド](docs/discord.md)
- [保存終了の証拠と制約](docs/evidence-qualified-shutdown.md)
- [既存のホストsystemd方式／初回Docker移行](docs/docker.md) — 互換運用向け。Compose更新手順とは別です。
- [移行データの保全](docs/migration.md)
- [ネットワーク](NETWORK.md) / [セキュリティ](SECURITY.md)

<details>
<summary>開発・検証</summary>

```bash
python3 -m compileall -q scripts
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m scripts.public_export
```

完全なSDK／Ansible試験は `requirements-discord.lock` と `deploy/requirements.txt` の依存を入れた Python 3.12／3.14 で行います。単体試験は合成データと偽transportを使用し、実際のDiscord／ゲームには接続しません。

独立コンテナのBotに `ARK_DISCORD_OFFLINE=1` を指定すると、token読込・SDK client・DNS／HTTP／Gateway・コマンド同期・投稿をせず、制限付きIPCのstatusだけを確認します。実際のDiscord認証／ユーザー操作の確認にはなりません。

公開exportはallowlist方式です。ゲーム、Saved、秘密ファイル、運用ログ、バックアップ、既存の私的Git履歴を含めないでください。

</details>

## ライセンス

本リポジトリのコードは [MIT License](LICENSE) です。ARK、SteamCMD、WorkshopのMOD、各依存物にはそれぞれの権利・利用条件が適用されます。
