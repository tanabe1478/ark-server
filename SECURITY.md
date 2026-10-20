# 公開・秘密・運転上の境界

## 公開してよいもの

ソース、テスト、Dockerfile、サービス雛形、ダミー値の設定例、一般化した手順です。運用実績を公開する場合も、個体情報・保存ファイル・生ログを含めません。

## 公開・ビルドコンテキストへ入れないもの

- `config/` 全体：Botトークン、guild/channel ID、実設定、ローカル検証policy
- `server/`、`steamcmd/`：ゲーム／MOD資産、Saved、認証INI、プロフィール、トライブ、許可Steam ID
- `logs/`、`backups/`：操作証拠、監査、設定を含む退避、個別検証記録
- `.git/`、実inventory、実secrets、`.env`、鍵、DB、ローカル接続情報、開発ツール

`.gitignore`はすでに追跡されたファイルや過去の履歴を消しません。`.dockerignore`はallowlistです。`git add -f`、広いCOPY、実INIを使ったCI試験、ログ／バックアップのartifact添付をしないでください。

## 秘密の渡し方

Composeの参加・管理者パスワードはowner-onlyの `config/compose/bootstrap.json`、受理済みの私的設定コピー、認証INIに保存します。Bot tokenは `config/compose/discord-bot.token` の専用ファイルだけです。旧ホスト方式のtoken pathは `config/discord-bot.token` です。トークンは設定例、引数、環境変数、Compose environment、ログへ書きません。BotはtokenとBot設定だけをread-onlyで受け取り、ゲーム認証やSavedは受け取りません。

GitHub Actionsに実Botトークン、実Discord ID、ゲームデータを登録しません。PRのコードを秘密付きで実行する`pull_request_target`を使用しません。イメージ公開も明示的な承認・ライセンス確認後の別作業です。

## 公開前の検査

`python3 -m scripts.public_export`は、公開候補のファイルを限定し、既知のローカル秘密との一致、トークン／秘密鍵形状、個人ホームパス、例示用でないIPv4などを確認します。値や該当行を出さず、ファイル名と分類だけを返します。

`--history`は過去のblobも確認します。検出があれば既存リポジトリをそのままpublicへ切り替えず、元を私的に保全し、履歴を含めない検査済みエクスポートを新しい公開リポジトリの候補にします。ツールの検査は漏えい不存在の証明ではありません。公開前には人による差分レビューと、必要に応じてgitleaks等でも確認してください。

秘密がすでに共有／pushされていた場合、削除だけでは失効しません。所有者と調整して失効・再発行／変更し、その後に履歴の扱いを決めます。稼働中のゲーム認証を無断で変更しません。

## 実行時の権限

- rootless Dockerだけを許可。BotへDocker socket・systemd bus・sudoを与えない
- IPCはowner-onlyのUnix socket。Composeでは同一UIDの別PID namespace peerだけを許可し、ゲームからの接続を拒否。TCP/HTTP listenerなし
- Bot用IPCはstatus/start/確認済みrestartと、時間制限したidle checkだけ。オーナー／更新用IPCは別volume
- ルートFS read-only、capability全削除、no-new-privileges、private PID namespace
- ゲームはLandlockで書込先をSaved・一時領域等へ限定し、観測証拠を書き換えられない
- ゲームに自動再起動policyなし。未知の終了／不確定な操作は遮断し、再送しない

Composeのtrusted updaterだけは専用rootless Docker socketを受け取ります。**read-only bindでもengine API権限を持ちます。** ゲーム／Bot／SteamCMD／候補ゲームへ継承しません。updaterは本番SavedをROで検査し、試験Savedを本番へ戻しません。公式mainの実行コード自動更新を有効にすると、その配布元のコード／buildを信頼することになります。詳細は [Compose信頼境界](docs/compose.md#構成と信頼境界) を参照してください。

同一ホストアカウント自体が侵害された場合まで、JSON・ハッシュで改ざん不能性を保証するものではありません。ネットワーク制御・OS更新・物理的なバックアップも別途必要です。

問題報告にはトークン、INI、生ログ、Saved、Steam ID、実IPを添付せず、まず再現条件・分類・ダミー値の最小例を使ってください。
