# 保存証拠付き警告停止

終了値6／134を正常扱いする設定ではありません。既知の保存・終了経路と実際のOS終了を観測し、異常終了記録を残したまま限定的に次の起動を許可します。

## 共通条件

- バイナリーSHA-256、観測profile、観測コード、固定runtime契約を照合
- boot／invocation／run ID、game／debugger／supervisorの実体を照合
- 一意の停止nonceをdurable保存してから停止要求
- 同一nonceで保存開始→保存完了→既知abort位置／caller→実ゲームSIGABRT/6を確認
- worldの更新・サイズと保存完了時メタデータが一致し、profile／tribeが失われず空でない
- 認証を含む設定ファイル**全体**が変わらず、全Savedの保存前後コピーとhashがある
- 実際のwriter終了とlifetime lock解放。OOM、timeout、別signal、不完全な観測／leaseを拒否
- 次回start時にも現在のSaved・設定・policy・runtimeを再検証

成立時も `clean_exit=false`、`expected_native_abort`、`in_game_content_verified=false` です。再起動ループ、`SuccessExitStatus`、`reset-failed`、abort抑止、binary patch、exit 0への変換を使いません。factoryのinactive/success/0へ戻っても過去の不確定な記録は無効になりません。

## Compose：永続コントローラーとschema4

ゲームだけが起動／停止するため、コントローラーコンテナの稼働とゲーム終了は別です。

- ネイティブ観測は実ゲームのsignal6。
- workerはそれを自分のOS signalとして再現し、親は実際の `wait` 結果 **-6** を保存。
- schema4はこのworker結果、run／PID／tracer、設定、画像内build identityとsource contract、保存前後の全ファイルを結合。
- cgroup v2 `memory.events` の起動前後のOOM counterも照合。不明・減少・取得不能をfalseにしない。
- 通常のゲーム停止後もコントローラーは稼働。構成全体のguarded終了では、警告を残すためコントローラー自身が134を返し得る。不明な障害は70等の別結果。

コンテナが終了していないのに「コンテナexit134を観測した」とは書きません。アップデーターは別途、実際のimage ID／container inspectと候補のbuild identity／source contractを結合します。

LinuxのPDEATHSIGは親のPIDだけでなく**作成したthreadの寿命**にも依存します。要求処理threadから直接spawnせず、コントローラーの持続threadがworkerを作成します。thread終了による実際のSIGKILLを終了0へ隠しません。

## Compose：新しいネイティブ配置の照合

`compose_profiles.py` は非PIE x86-64 ELFを限定解析し、保存開始・保存完了・forced abort・exit callerの4つの一意な実行命令fingerprintを探します。アドレス／relocation／相対call operandだけを正規化し、任意の命令や制御フローを無視しません。

`abort` PLT、caller→abort関数、保存開始の呼出関係、保存点の近接／分離なども確認します。未知・曖昧な配置は本番を止める前に不合格とし、旧版を維持します。

命令照合だけでは更新を承認しません。最新Savedの独立コピーで実際の準備完了→保存終了→再読込を2回確認し、正確な候補assets／runtime／policy／保存受領証を照合して初めて適用候補になります。すべての将来buildを自動サポートする意味ではありません。

通常起動前のINI意味比較は、管理対象の数値の同値表記／既定1.0の省略と、engineが追加する既定値を許容します。それ以外の元エントリー、重複Unreal配列、認証、MOD順序は保持します。この比較で、停止受領証の全設定hashを緩めません。

## 互換：Native／ホストsystemd Docker

旧ホスト方式はASE固定buildのprofileを使用します。対応バイナリー／定数は `ark_shutdown.py` にあります。

Native bridgeは実ゲームのsignalを再現し、systemdのfailed/core-dump/6を保持します。ホストrunner方式のDockerではTiniが子のsignal終了を134で表し、host runnerも134を返すため、systemdはfailed/exit-code/134です。schema2のprivate receiptには実ゲームsignal6とコンテナ／ホスト134を別々に保存します。コンテナ134だけのunknown crashは許可しません。

初回Native→Docker切替の許可と、その後のCompose更新transitionは別です。中断markerや過去の失敗を削除して変換しません。

## コンテナでの観測開始

Steam初期化を最初からGDB下で実行するとSIGTRAPを観測する比較ケースがありました。ソース上の理由は未確定で、そのsignalを握りつぶしていません。

GDBを実親としてまずtracerなしで起動し、設定に合うA2S応答後、自分の子へattachします。保存／abort観測が完了するまで準備済みとは判定しません。未知のstartup終了を保存証拠にしません。観測親の喪失はPDEATHSIGで孤立writerを防ぎ、旧host runnerではlease喪失も別に記録します。

通常のreadiness timeoutだけで、稼働中ゲームをkill／再起動しません。不明な効果を再送せず、台帳とデータを残して保留します。

## 制約

ソース関数名・不具合原因を完全に確定したものではありません。ネイティブ命令構造と実際の保存／終了に依存し、別ゲーム・未知の配置・不完全な結果へ流用しません。

A2S、ネイティブ保存完了、ファイルhash、2回の再読込でも、建築物・恐竜・アイテム・MOD内容の完全性は保証できません。所有者のゲーム内確認が必要です。receipt、生ログ、Saved、個体情報は公開せず、privateなvolume／ignored領域へ保持します。
