# Compose運用と復旧

通常の入口はリポジトリ直下の `compose.yaml` です。ホストsystemd／初回Native→Docker移行用の `docker_setup.py` とは別の方式です。既存運用へ無断でComposeを重ねて起動しないでください。

## 構成と信頼境界

| service | 責務 | 主な権限 |
|---|---|---|
| `platform` | 更新対象外の安定したplatform終了証人 | 専用lease volume RW、終了証拠 RO。network／Saved／token／engine socketなし |
| `setup` | 明示的な初期取得／import、停止設定保守 | assetsと初期Saved。token／engine socketなし |
| `server` | 永続コントローラーと、起動／停止するゲーム子プロセス | assets RO、Saved RW、観測と保全。engine socketなし |
| `bot` | 外向きDiscord通信、制限されたゲーム操作 | Bot設定／token RO、Bot用IPC、専用lockだけ |
| `updater` | 取得、候補検証、条件付き差し替え | 専用rootless socket、assets、Saved **RO**、保全／更新台帳 |
| `manager` | オーナー向けのstatus/start/stop/restart | 管理IPCのみ、networkなし。tools profile |

全サービスはrootless engine内のUID0です。これはホストrootではなく、engineを所有する非特権UIDへ写像されます。read-only rootfs、cap-drop ALL、no-new-privileges、標準seccompを維持します。

**socketのread-only bindはDocker API権限を弱めません。** updaterはホスト所有者相当の信頼が必要な保守プレーンです。公式mainのコード更新を有効にすることは、その配布元のコードとビルドを信頼する選択です。fork／独自変更の検証は明示オーナー操作で行ってください。

SteamCMD、ネイティブ配置probe、候補ゲーム、適用処理は個別の制限付きコンテナです。SteamCMD／候補ゲームには本番Saved・Bot token・engine socketを渡しません。適用処理も本番SavedはROです。検証用Savedの中にはゲーム用認証情報があるため、stagingも秘密データとして扱います。

ゲームはLandlockでSaved・一時領域等だけを書き込み可能にします。Bot用と管理者用のIPCは別volumeです。peer UIDに加えPID namespaceの関係を検査し、同じゲームコンテナからの接続を拒否します。

## rootless接続先

Compose CLIとupdaterが同じengineを参照する必要があります。

標準rootless環境では、選択済みrootless contextと次のsocketを使います。

```bash
export ARK_DOCKER_SOCKET="$XDG_RUNTIME_DIR/docker.sock"
docker info --format '{{json .SecurityOptions}}'
docker compose up -d
```

独立した専用engineを利用する例：

```bash
export DOCKER_HOST="unix:///run/user/$(id -u)/ark-docker.sock"
export ARK_DOCKER_SOCKET="/run/user/$(id -u)/ark-docker.sock"
docker compose up -d
```

これはその端末の環境変数です。既定context・rootful socketの権限・Dockerグループ・systemdのlinger設定を変更する処理はありません。毎回 `docker --host ... compose ...` と明示しても構いません。

同じprojectの稼働希望状態はvolumeに保存します。`auto_start` は初回値で、`manager stop`／無人停止後に `up -d` しただけではゲームを再開しません。

## ホスト起動時の自動起動

`platform`／`server` は `restart: "no"` です。ホストやengineを再起動すると、**`docker compose up -d` を再度実行するまでBotもコントローラーも戻りません**。常時運用では、初回の `docker compose up -d` と受入確認が済んでからsystemd user unitを配置します。

接続先とproject名は、リポジトリ直下の `.env`（Git対象外）に固定します。秘密値は書きません。

```bash
# .env の例（値は環境に合わせる）
COMPOSE_PROJECT_NAME=ark
ARK_DOCKER_SOCKET=/run/user/1000/docker.sock
ARK_GAME_PORT=7777
ARK_GAME_PORT_PLUS_ONE=7778
ARK_QUERY_PORT=27015
```

`COMPOSE_PROJECT_NAME` は既存volumeの名前を決めます。稼働中の値から変えないでください。unitは端末の `DOCKER_HOST`・`COMPOSE_PROJECT_NAME` を引き継がず、`.env` とテンプレートに書いたsocketだけを使います。

```bash
export ARK_DOCKER_SOCKET="$XDG_RUNTIME_DIR/docker.sock"   # 専用engineならそのsocket
ENGINE_UNIT=docker.service                                # 専用engineなら ark-docker.service
mkdir -p ~/.config/systemd/user
( set -o noclobber
  sed -e "s|@ROOT@|$PWD|g" -e "s|@ENGINE_UNIT@|$ENGINE_UNIT|g" -e "s|@DOCKER_SOCKET@|$ARK_DOCKER_SOCKET|g" \
      deploy/docker/ark-compose.service.in > ~/.config/systemd/user/ark-compose.service )
grep -n '@' ~/.config/systemd/user/ark-compose.service || echo 'placeholders: ok'
systemd-analyze --user verify ark-compose.service
systemctl --user daemon-reload
systemctl --user enable ark-compose.service
```

`noclobber` により、同名unitがあれば上書きせず失敗します。既存unitは中身を確認し、手で統合してください。**同じprojectを複数のunitから起動しないでください。** `enable` だけでは今の構成に触れません。次回のuser manager起動時から有効です。

- `ExecStart` は `up -d --no-build` です。build・pull・volume削除はしません。イメージは初回 `up -d` かupdaterが用意したものを使います。
- `ExecStop` は `-v` なしの `compose down` で、各serviceの25分の保存終了猶予を待ちます。
- 稼働希望状態はvolume側にあります。`manager stop` や無人停止の後なら、ホストを再起動してもゲームは停止したままで、Botとコントローラーだけが戻ります。
- user managerは、ログインするかlingerを有効にした場合にだけ起動します。無人のPCで再起動後に戻したい場合は、所有者が `loginctl enable-linger "$USER"` を選択してください。このプロジェクトはlingerを変更しません。PCの電源投入・スリープ解除・停電復旧は対象外です。

## 既存データのimport

**停止して保存終了を確認したデータの独立コピー**を準備します。稼働中Savedの無保護コピーや、過去の移行パッケージを新しい進行へ上書きする操作ではありません。

```text
private-import/
  server/   # ゲーム・MOD。ShooterGame/Savedはコピー対象外
  Saved/    # 保存済みワールド、profiles、tribe、Config等
```

1. 旧サーバーをその**旧版の検証済み方法**で保存終了。
2. 設定・履歴・最新Savedを別に保全。未知の終了を終了0に正規化しない。
3. `bootstrap.json` に元のマップ、人数、MOD順序、倍率、異なる参加／管理者パスワードを正しく設定。
4. 初回だけ `auto_start: false` を推奨。
5. **新しい空のCompose project／volume**へimport。

```bash
export ARK_IMPORT_DIRECTORY="$(realpath private-import)"
docker compose run --no-deps --name ark-initial-import setup --import-data
docker compose up -d
```

`setup` はsource inventoryの変化を確認し、installerや移行パッケージのstartスクリプトを実行しません。既存Saved、初期化中断marker、既存launch ledgerがある場合は再初期化しません。失敗したvolumeを消して同じ移行を成功扱いにするのではなく、原本と失敗記録を残し、原因を確認してください。

新規設定のパスワードは12～128文字です。ただし、明示importで元のINIと完全一致する既存認証を引き継ぐ場合は、対応文字の1～128文字をそのまま保持できます。この例外を新しい短いパスワードの設定には使えません。受理済みimportの通常up／設定保守でも現行INIとの一致を再確認し、認証情報を黙って変更しません。

INIの既存配列・重複エントリーは保持して検査します。管理対象の数値は型付き値で比較するため、同値の表記／既定1.0の省略は許容します。その他の既存値、認証、MOD順序、loot配列の変更を自動で無視しません。受領証の設定ファイル全体のハッシュは、この意味比較とは別に厳密です。

最初の起動は非公開の保存終了検証を含みます。A2S・自動再読込が通っても、移行内容の完全性を保証しません。キャラクター、トライブ、建築物、恐竜、アイテム、MODと外部参加は別途確認します。旧運用の停止状態・証拠は保ったまま切り替えてください。

## 設定変更

ホストの公開UDPポートはComposeの非秘密環境変数で設定します。コンテナ内部のゲーム7777／query27015は固定です。

`bootstrap.json` の編集を既存Savedへ暗黙適用しません。通常の設定変更は次の明示保守です。

1. 更新中でないことをstatusで確認。必要に応じゲームを `manager stop` で停止希望状態にする。
2. `docker compose down` **（`-v`なし）**で受理済み処理と保存終了を待つ。
3. 既存のbootstrapを検証するsetupを一度実行し、受理済み入力の私的コピーを確保。既存版からの初回アップグレードでも必要。
4. ファイルを編集し、明示コマンドで適用。

```bash
docker compose run --no-deps setup
${EDITOR:-nano} config/compose/bootstrap.json
python3 -m scripts.compose_init --check
docker compose run --no-deps --name ark-owner-configuration setup --configure-existing --confirm
docker compose up -d
```

`backups/compose-config-<id>/` に変更前後を保全します。変更前の正常／警告付き停止と同じruntime／policyであること、変更前の非INI進行が変わっていないことを検査します。新しいINIで公開起動する前に、実際の非公開保存終了を再度要求します。元の障害latchは消しません。

マップやMOD構成の変更は通常のバージョン更新とは別です。未配置の新規MODは、設定変更前に拒否します。このコマンドはMOD installerを実行したり、未知のassetsを無検証で本番へ追加したりしません。初期配置／別ステージで準備したassetsの明示importを使い、ゲーム内互換性も確認してください。

### オン／オフの設定（options）

bootstrap に任意の `options` を書くと、決まったオン／オフ項目を管理します。書かない項目は既存INIの値をそのまま残します。

| キー | INI | 内容 |
|---|---|---|
| `show_map_player_location` | `ShowMapPlayerLocation` | 地図に自分の位置と向きを表示 |

```json
"options": {"show_map_player_location": true}
```

適用は上の設定変更の手順（`setup --configure-existing --confirm`）です。

### マップの切り替え

ゲーム本体に含まれる公式マップ（`TheIsland` `TheCenter` `ScorchedEarth_P` `Ragnarok` `Aberration_P` `Extinction` `Valguero_P` `Genesis` `CrystalIsles` `Gen2` `LostIsland` `Fjordur` `Aquatica`）へ、MOD・倍率・パスワード・Discord設定はそのままで新しいワールドとして切り替えます。前のワールド・キャラクター・トライブ（`Saved/SavedArks`）は、次のどちらかを選びます。

- `--data backup`: `backups/compose-map-<id>/before/` に退避してから消す。あとで戻すときの元になる
- `--data delete`: 退避せずに消す。元に戻せない

設定・INI・MODのデータ（`SaveGames`）・ログは残ります。手順は設定変更と同じで、bootstrapでは `settings.map` だけを変えます（ほかの項目も変わっていると拒否します）。

```bash
docker compose run --rm --no-deps manager stop   # 動いていれば
docker compose down                              # -v なし
${EDITOR:-nano} config/compose/bootstrap.json    # settings.map だけ変える
docker compose run --rm --no-deps setup --switch-map Genesis --data delete --confirm
docker compose up -d --no-build
```

更新ジョブの途中・保守の中断が残っている・直前の停止を検証できないときは、何も消さずに拒否します。拒否されたら bootstrap の `settings.map` を元に戻してから `up` してください（戻さないと setup が入力の変更を検出して起動を止めます）。最初の起動では、新しいマップで非公開の保存・終了を確認してから公開します。

### 設定保守の中断

`config/compose-config.pending.json` が残ると起動を保留します。明示的な設定復旧：

```bash
docker compose run --no-deps --name ark-owner-config-recovery setup --restore-configuration --confirm
```

これは同じruntime・同じ過去invocation・変更されていない非INI進行を確認し、現在の状態も追加保全してから、**設定とINIだけ**を元へ戻します。ワールド、profiles、tribeは戻しません。より新しいinvocation／進行がある場合は拒否します。

ホストの編集済みbootstrapはread-only bindなので書き戻しません。受理済みの私的ファイルを、保持した復旧コンテナから取り出して元へ戻せます。

```bash
docker cp ark-owner-config-recovery:/opt/ark/config/bootstrap.accepted.json config/compose/bootstrap.json
chmod 600 config/compose/bootstrap.json
python3 -m scripts.compose_init --check
docker compose up -d
```

## 更新と復旧

通常は `updates.game/mods/images: true` のままで定期確認します。MODは設定済みIDの更新です。公式mainの実行コード／依存ロック／Composeイメージ定義に変更があると、正確なcommitを取得してローカルbuildします。buildにはmutable aptパッケージ取得も含まれるため、bit-for-bit再現可能なbuildとは主張しません。イメージIDとパッケージ一覧を保持します。

```bash
docker compose exec updater python3 -m scripts.compose_update status
docker compose exec updater python3 -m scripts.compose_update check
```

`check` は非同期要求です。要求IDと更新jobを台帳へ保存します。現在版を実際の2-cycle／差し替え経路で再検証するオーナー操作もあります。

```bash
docker compose exec updater python3 -m scripts.compose_update validate-current --confirm
```

稼働中なら通常と同じ人数・猶予の条件を満たす必要があります。停止中なら本番を起動しません。検証コピーだけが起動します。

### 主な状態

| phase | 扱い |
|---|---|
| `no_update` / `disabled` | 変更なし／確認対象なし |
| `deferred` | 人数・猶予・メモリー・ディスク・platform停止などで保留 |
| `candidate_rejected` / `candidate_blocked` | 非対応配置・不合格候補。旧assets／進行を維持 |
| `qualifying` | 最新Savedコピーで実際の2-cycle検証中 |
| `applying` | 旧設定／必要な旧assets保全後の適用中。中断を暗黙にやり直さない |
| `commit_requested` | 送信前にdurable記録済み。応答不明なら再送しない |
| `handoff` / `handoff_replacing` | 信頼済み保守helperへupdater自身の交換を引継ぎ |
| `completed` | 適用と必要な引継ぎが完了 |
| `platform_stopped` | 適用後のplatform停止を確認。未完の保守引継ぎをcompletedへ変換しない |
| `blocked` | 不明な効果・不完全な証拠。自動再試行しない |
| `owner_closed_interrupted` | 所有者が中断事実を保全してjobを終結。ゲームの未知の終了・latchは維持し、再送／起動を承認しない |

非公開の試験／初回資格確認では、起動前のprofile／tribeの消失・空化と、ワールドの10%を超える大幅縮小も拒否します。これは重大な退行の検出であり、ゲーム内容の完全性証明ではありません。通常プレイ中の正当な進行変化に、この起動試験用のサイズ比較を当てません。

公式Workshopのdata cookはLinuxサーバーでも `WindowsNoEditor` を使用します。対応cookがないMODは拒否し、`LinuxNoEditor` へ暗黙にfallbackしません。ヘッダー生成／解凍だけでは互換性を保証しません。

ネイティブ配置probeは本番停止より前に行います。命令fingerprintが合うことだけでは承認せず、実データコピーの2-cycle証拠も要求します。新compiler／構造変更は旧版を維持して不合格になります。

適用前に旧版で止め、候補のinput Saved／設定／assets／image／保存受領証を再照合します。ワールドを古いコピーで置き換えません。適用中の本番Saved mountはROです。

ゲーム／MODが変わらないソースのみの更新では、候補probe／2-cycleへ現行program volumeをROで渡します。適用helperもカーネルRO mountを必須とし、programの複製・削除・再配置を行わず、完全ハッシュ一致とversion不変を検査します。旧設定とprogram manifestは保全します。Savedは従来どおり独立コピーで検証し、trialを本番へ返しません。ゲーム／MODを変える場合は、別assetsの配置と旧programの実コピー保全を省略しません。

未知のnative crash、OOM、timeout、observer喪失、途中の試験終了は不完全な証拠として保留します。ゲームを一度も起動していないと証明できる候補の事前失敗だけは、未変更の旧版を共通guard経由で再開できます。readiness timeoutを理由に本番ゲームや試験ゲームをkill／再起動しません。試験側に生存中／不明の子がある場合は親helperも保持し、updaterをblockedにします。稼働helperが残る間のrecoverは拒否されます。ログ・実プロセス・Savedを所有者が確認するまで、自動終了や再実行は行いません。

### 中断したjobのオーナー復旧

私的台帳・候補コンテナ・受領証・最新進行を確認した後、**正確なjob ID**を指定します。

```bash
docker compose exec updater python3 -m scripts.compose_update recover JOB_ID --confirm
```

対応するpending／保全が残り、同じjobのhelperが動いておらず、より新しいlaunchがなく、commit送信が記録されていない場合だけ、未変更旧版の中止または保全付き再適用を行います。試験Savedの本番復元はしません。

commit送信後の応答不明や、より新しい進行がある場合は自動復旧対象外です。marker削除、`reset-failed`、台帳削除、古いSavedの復元で通過させないでください。必要なインフラ交換だけの復旧でも、ゲームのcommitを再送しないことと、現在のコンテナ／受領証の確認が必要です。

ホスト再起動でpost-commitの起動が不明になったjobを、再送せずに所有者が終結する専用操作もあります。

```bash
docker compose run --no-deps --entrypoint /usr/bin/python3 updater -m scripts.compose_review seal-interrupted JOB_ID --confirm
```

実適用記録・ローカルcommit受領・現在runtime・正確な交換後container ID／image・前bootの未確定invocation・実停止・helper不在を要求し、現在Savedと元記録を新しい宛先へ保全します。結果は `owner_closed_interrupted` であり、成功／clean exitではありません。commit／適用／起動は再送せず、元のゲーム状態・automation latch・未知の終了を変更しません。新たな起動許可も発行しません。同一bootの不明状態や生存helperはこの操作で通過させられません。

ゲームを再開するためのデータ引継ぎは別の明示判断です。直前の資格済みinputとのworld／profile／tribe比較、独立コピーでの保存再読込と実ゲーム内確認を行い、現在データが不明なら引き続き保留します。終結操作を古いSavedへの自動rollbackや起動承認の代わりには使わないでください。

`compose down` と交換が競合すると、Composeが最初に列挙した旧container IDがすでに無い場合があります。このため、updaterが交換しない `platform` 証人を別serviceとして常設します。署名の代わりになるものではなく、専用volumeのowner-only generation／boot／heartbeatと明示closing状態です。新しいコントローラーはclosing中の起動を拒否し、現コントローラーの実際の保存終了後に同じgeneration／hostname／broker run IDのdrain記録を作ります。証人はそれを待って終了します。未知のlease失効は別のdurable holdとなり、自動再開しません。

この安定証人は `*-platform:local` という独立image tagを使い、通常のsource更新でserver tagが変わっても、次の `up -d` だけで証人を交換しないようにしています。証人自身の更新は、ゲームの保存終了を確認して構成全体をdownしたうえで、所有者が `docker compose build platform` → `docker compose up -d` とする計画保守です。通常の自動更新対象はserver・Bot・updaterで、安定証人のprotocolを暗黙に交換しません。

この安定証人と永続job停止marker・新しいコントローラー・updater引継ぎを調整し、交換後のサービスも通常の保存終了で止めます。強制kill、engine消失、証拠書き込み不能まで正常完了と扱うものではありません。

### ローカルソースの明示検証

公式main以外のソースは、自動取得元の設定を黙って変えず、オーナーが公開allowlist形式のexportを用意します。`public_export --destination NEW_DIRECTORY` で秘密情報を含まない束を作り、そのrelease digest名で `staging` volumeの `source/<digest>/` に配置します。

```bash
docker compose exec updater python3 -m scripts.compose_update validate-source source/RELEASE_DIGEST --confirm
```

任意のパスやsymlinkではなく、範囲・ファイル数・容量・公開scanner・内容digestを検査します。その後は通常と**同じ**イメージbuild、ネイティブprobe、人数／猶予、最新Savedコピー、2-cycle、適用、引継ぎを使います。Botから実行できません。通常の公式main更新に個別承認は要りません。

## 障害を調べる

- `docker compose logs server updater bot`
- `evidence` volumeの `logs/process/`・`logs/shutdown/`・`logs/automation/`・`logs/updates/`
- `backups` volumeの保存前後、update-input、旧assets、旧container inspect／console
- stagingの検証結果・failure・保持された候補コンテナ

これらはprivateです。トークンを直接ログへ出さなくても、Saved／INI／パス／操作主体のIDを含み得ます。issueへ丸ごと貼らず、秘密値・個人情報を除いた必要最小限を共有してください。

実際のゲーム終了はsignal6、workerのwait結果は-6、コントローラーはゲーム停止後も稼働、構成全体の通常終了時は警告を残すため134となり得ます。それぞれ別の事実です。`SuccessExitStatus` を足したり、終了0へ変えたりしません。

## リリース時の検証範囲と制限

独立したrootless環境で、公式Steamからの新規取得、公式Workshopの8MOD展開、Aquaticaの非公開保存終了→公開A2S待受け→保存終了、同一invocationへのno-op start、restart、設定のみの復旧とworld不変を確認しました。最新Savedコピーでの実際の2-cycle／progress-shape検証、停止希望を保持した適用・3サービス交換・updater引継ぎも確認しています。旧版の30分待機上限に達した適用は不明として保留され、helperの実終了・Saved不変・新規起動なしを確認した後の明示的な所有者復旧で完了しました。現在の適用待機上限は2時間です。その後のソースのみ更新はprogram RO再利用へ変更し、実稼働中の更新→再開→3サービス交換／updater引継ぎを自動完了しました。実測例ではprogramコピー0 bytes、適用3分9秒（旧コピー経路の観測は約33分）、確認開始から完了まで14分56秒でした。ディスク／キャッシュ／worldに依存する一例であり、瞬時の更新を保証しません。ゲーム／MOD変更時の実コピー保全や、Savedコピー・実読込・完全ハッシュ照合は引き続き必要です。

終了競合は、実際のComposeが旧IDを列挙した後のDocker API送信を所有者限定Unix proxyで一時保留し、その間に同一の検証済みimageでserver・Bot・updaterを交換して再び公開待受けさせる試験で再現しました。保留を解除すると、旧IDが消えた後でも安定証人が新しいサービスを終了させ、nativeの実保存終了と現在Savedのafter-snapshot一致を確認しました。これは任意のengine喪失やupdaterのSIGKILLまで含む無条件のno-orphan保証ではありません。

別の稼働中ソース更新では2-cycleと適用が通った後、commit送信後の非公開起動中にホストが再起動しました。そのjobは成功とせず、適用／ローカルcommit受領／前bootの不明起動と実停止を結び、所有者が現在Savedと記録を保全して `owner_closed_interrupted` として終結しました。commit／起動を再送せず、未知のゲーム終了とautomation latchは保持しています。この終結を起動承認や旧Saved復元の代わりには使えません。

Python 3.12／3.14で依存関係を含む331テストを確認しました。Discordは実Gateway接続・指定guildへのアクセス・既存の `/ark` 登録を読み取り確認しましたが、この確認では同期・メッセージ送信・ゲーム操作を行っていません。実メンバーによるslash／確認ボタン、外部参加、ゲーム内／MOD内容は[具体的な受入項目](acceptance.md)で確認し、自動テストの結果と混同しません。完全な任意動作や将来のASEビルドの保証ではありません。
