# Mac-Junk-Cleaner

macOS のディスク使用量を調べ、再生成できるキャッシュを削除する Python CLI ツールです。  
Chromium のデータ構造をもとに未知の Electron アプリも検出し、Chrome の各プロファイルを含めて使用容量を表示します。  
デフォルトでは削除候補を表示します。実際に削除するには `clean --apply` を指定し、確認プロンプトで `DELETE` と入力してください。

## インストール

macOS、Python 3.11 以降、および uv が必要です。

```bash
cd ~/Develop/Mac-Junk-Cleaner
uv sync
uv run mac-junk-cleaner --help
```

別のディレクトリから実行する場合は `uv run --project ~/Develop/Mac-Junk-Cleaner mac-junk-cleaner scan` を実行してください。

## 容量の分析

```bash
uv run mac-junk-cleaner analyze --json disk-usage.json
uv run mac-junk-cleaner analyze --root ~/Develop --depth 3
```

デフォルトでは内蔵の Data ボリュームをスキャンします。  
ネットワークドライブ、追加のマウントポイント、シンボリックリンク、`Volumes` ディレクトリはスキャン対象から除外します。  
読み取り権限がないパスはエラーとして記録されるため、スキャン結果の詳細を JSON で確認できます。

読み取り処理が10秒を超えたディレクトリもエラーとして記録します。  
同一の一時ディレクトリ内で処理が停止する場合は、該当する一時領域の残りの探索をスキップし、他の領域のスキャンを続行します。

表示される割り当て容量は、ファイルが確保しているブロックの合計です。  
APFS の共有ブロックやスナップショットの影響があるため、この合計値と実際のディスク使用量は異なります。  
親ディレクトリの容量には子ディレクトリも含まれているため、一覧の各行を合算した数値をそのまま削除可能な容量とみなさないでください。

## 候補の確認とクリーンアップ

まずは全体をスキャンして確認し、目的に応じてカテゴリやパスを絞り込んで実行することをおすすめします。

```bash
uv run mac-junk-cleaner scan --json cleanup-candidates.json
uv run mac-junk-cleaner scan --category apps
uv run mac-junk-cleaner scan --verbose
uv run mac-junk-cleaner clean --dry-run --manifest cleanup-files.jsonl
sudo .venv/bin/mac-junk-cleaner clean --apply
sudo .venv/bin/mac-junk-cleaner clean --category apps --apply
sudo .venv/bin/mac-junk-cleaner clean --path "$HOME/Library/Caches/Google" --apply
```

デフォルトでは、アプリのキャッシュ、パッケージキャッシュ、プロジェクト形式から判別できるビルド生成物を削除対象にします。  
シングルユーザーの Mac を前提として、`/Library/Caches` 、Logi Options+ のキャッシュ、Lark の更新ファイルも含めます。

`clean --dry-run` は削除予定の全パスを表示します。  
`--manifest` を併用すると、パスごとの種類・容量・カテゴリを JSON Lines 形式で保存できます。  
起動中のアプリはキャッシュを再生成するため、一覧の作成時と削除時で内容が変わる場合があります。

通常の表示はカテゴリごとの合計容量と容量上位20件です。  
`--verbose` または JSON 出力で、全件のパスと削除による影響を確認できます。

削除は `sudo .venv/bin/mac-junk-cleaner clean --apply` で実行してください。  
sudo で実行した場合も、元のユーザーのホームディレクトリとパッケージ管理ツールを使います。  
確認済みの対象を非対話で削除する場合は、`--apply --yes` を指定できます。  
ファイルはゴミ箱を経由せず直接削除されるため、元に戻すには再取得や再ビルドが必要です。

### パッケージキャッシュ

パッケージの保存先は標準パスに加えて、インストール済みツールへの問い合わせや環境変数から特定します。  
パッケージキャッシュもデフォルトで削除対象に含まれるため、追加フラグは不要です。  
uv のキャッシュについても公式の `uv cache clean` を実行し、ダウンロード済み・ビルド済みのキャッシュ全体を削除します。  
次回の利用時に再取得や再ビルドが必要になります。uv の symlink モードで作成した環境では、依存パッケージの再インストールも必要です。

### 起動中のアプリと保持するデータ

アプリを起動したままでもクリーンアップを実行できます。  
プロセスが開いているファイルや即座に再生成されたキャッシュが残る場合があるため、処理後に残った容量と実際の空き容量の増加分を表示します。  
対象のキャッシュディレクトリ自体は残し、その中のファイルとサブディレクトリを削除します。

履歴、Cookie、IndexedDB、Local Storage、設定、拡張機能本体は保持します。  
ダウンロードフォルダや音声モデルの通常の保存先は、容量分析だけの対象です。  
ただし、アプリのキャッシュ領域にもモデルやオフライン素材が配置される場合があるため、容量の大きな候補についてはパスと再取得による影響を確認してください。

## 追加の対象

通常のキャッシュ削除だけでは空き容量が不足する場合、オフライン用データや古い依存パッケージを対象に追加できます。  
一覧にはこれらの追加対象も表示されますが、削除対象に含めるにはそれぞれのフラグを指定する必要があります。

```bash
sudo .venv/bin/mac-junk-cleaner clean --category cache-storage --include-cache-storage --apply
sudo .venv/bin/mac-junk-cleaner clean --category stale-node-modules --include-stale-node-modules --apply
```

`CacheStorage` は Web サイトが保存したリクエストとレスポンスのキャッシュであり、オフライン利用などに使われます。  
削除後は再取得が発生し、起動中のサイトや Electron アプリではリロード（再読み込み）が必要になる場合があります。  
対象のサイトが現在も利用されているかは更新日時から判断できないため、削除対象とするアプリやプロファイルをパスで指定してください。

古い `node_modules` は、デフォルトではソースコードや Git メタデータの最終更新から183日以上経過したプロジェクトが対象です。  
ロックファイルが存在し、依存パッケージ配下のファイルが Git で追跡されていないことも確認します。  
この判定は最終更新日からの推定に基づくため、コードの閲覧のみを行った場合や、`node_modules` を手動で修正したようなケースは判別できません。

プロジェクトのルートディレクトリや日数はオプションで指定できます。

```bash
uv run mac-junk-cleaner scan --project-root ~/Projects --stale-days 365
```

## 開発

パッケージのソースコードはリポジトリ直下の `mac_junk_cleaner/` に配置されています。  
`uv sync` を実行すると、Ruff、Pyright、taskipy もインストールされます。

```bash
uv run task lint
uv run task format
uv run task test
uv build
```

## 参照

- [uv のキャッシュ管理](https://docs.astral.sh/uv/concepts/cache/)
- [CacheStorage の用途](https://developer.mozilla.org/en-US/docs/Web/API/CacheStorage)

## ライセンス

[MIT License](License.txt)
