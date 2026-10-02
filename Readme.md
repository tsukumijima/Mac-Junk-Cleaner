# Mac-Junk-Cleaner

macOS のディスク使用量を調べ、再生成できるキャッシュを選んで掃除する Python CLI です。  
未知の Electron アプリも Chromium の保存構造から探し、Chrome の各プロファイルを含めて容量を表示します。  
通常の実行はプレビューで、`clean --apply` を指定すると対象と影響を表示してから削除を確認します。

## インストール

macOS と Python 3.11 以降、uv が必要です。

```bash
cd ~/Develop/Mac-Junk-Cleaner
uv sync
uv run mac-junk-cleaner --help
```

別のフォルダから使う場合は `uv run --project ~/Develop/Mac-Junk-Cleaner mac-junk-cleaner scan` を実行してください。

## 容量の分析

```bash
uv run mac-junk-cleaner analyze --json disk-usage.json
uv run mac-junk-cleaner analyze --root ~/Develop --depth 3
```

標準では内蔵 Data ボリュームを調べます。  
ネットワークドライブ、追加マウント、シンボリックリンク、`Volumes` ディレクトリは探索から除外します。  
読み取り権限がない場所はエラーとして記録するので、測定範囲を JSON で確認できます。

読み取りが10秒を超えるディレクトリもエラーとして記録します。  
同じ一時フォルダ内で読み取りが止まる場合は、その一時領域の残りを除外し、ほかの領域を測定します。

表示する割当容量はファイルが確保しているブロックの合計です。  
APFS の共有ブロックやスナップショットがあるため、合計とディスクの使用量は異なります。  
親ディレクトリの容量には子も含まれるので、一覧の各行を足した値を削除可能な容量として扱わないでください。

## 候補の確認と掃除

最初は全体を確認し、用途に合わせて分類やパスを絞る使い方をおすすめします。

```bash
uv run mac-junk-cleaner scan --json cleanup-candidates.json
uv run mac-junk-cleaner scan --category apps
uv run mac-junk-cleaner scan --verbose
uv run mac-junk-cleaner clean --dry-run --manifest cleanup-files.jsonl
sudo .venv/bin/mac-junk-cleaner clean --apply
sudo .venv/bin/mac-junk-cleaner clean --category apps --apply
sudo .venv/bin/mac-junk-cleaner clean --path "$HOME/Library/Caches/Google" --apply
```

通常の対象は、アプリのキャッシュ、パッケージキャッシュ、プロジェクト形式から判別できるビルド生成物です。  
`clean --dry-run` は削除予定の全パスを表示します。  
`--manifest` を併用すると、種類・容量・候補の分類を含む一覧を JSON Lines で保存します。  
一覧は測定時点の予定で、起動中のアプリによる再生成で実行時の内容が変わる場合があります。  
シングルユーザーの Mac を前提に、`/Library/Caches` 、Logi Options+ のキャッシュ、Lark の更新ファイルも標準の対象に含めます。  
削除は `sudo .venv/bin/mac-junk-cleaner clean --apply` で実行してください。  
sudo 起動時も元のユーザーのホームとパッケージ管理ツールを使います。  
標準の表示は分類合計と容量上位20件です。`--verbose` または JSON 出力で全件のパスと影響を確認できます。  
パッケージの保存先は標準パスに加えて、導入済みツールへの照会と環境変数から確認します。  
パッケージキャッシュは追加フラグなしで標準の削除対象に含まれます。  
uv も公式の `uv cache clean` を実行し、ダウンロード済み・ビルド済みのキャッシュ全体を削除します。  
次回は再取得・再ビルドが必要で、uv の symlink モードで作成した環境は再インストールが必要です。

アプリは起動したまま掃除できます。  
開いているファイルや再作成されたキャッシュが残る場合があるので、処理後の残量と実際の空き容量の増加を表示します。  
キャッシュディレクトリ自体を残し、中のファイルを削除する構成です。

履歴、Cookie、IndexedDB、Local Storage、設定、拡張機能本体は保持します。  
ダウンロードや音声モデルの通常の保存先は分析だけの対象です。  
ただし、アプリのキャッシュ領域にもモデルやオフライン素材が置かれることがあるため、大きな候補はパスと再取得への影響を確認してください。

## 追加の対象

通常のキャッシュだけで不足するときは、オフライン用データや古い依存パッケージを追加できます。  
一覧では追加対象も表示しますが、削除対象へ加えるにはそれぞれのフラグが必要です。

```bash
sudo .venv/bin/mac-junk-cleaner clean --category cache-storage --include-cache-storage --apply
sudo .venv/bin/mac-junk-cleaner clean --category stale-node-modules --include-stale-node-modules --apply
```

`CacheStorage` はサイトが保存したリクエストとレスポンスで、オフライン利用に使われます。  
消去後は再取得が必要で、起動中のサイトや Electron アプリは再読み込みが必要になる場合があります。  
対象のサイトを使っているかは更新日時から判断できないため、削除するアプリやプロファイルをパスで選択してください。

古い `node_modules` は、標準ではソースと Git メタデータの最新更新から183日以上経過したプロジェクトが対象です。  
ロックファイルがあり、依存パッケージに Git の追跡ファイルがないことも確認します。  
これは最終利用日の推定で、閲覧だけの利用や `node_modules` の手修正は判別できません。

開発フォルダや日数は指定できます。

```bash
uv run mac-junk-cleaner scan --project-root ~/Projects --stale-days 365
```

削除確認は `DELETE` の入力で進みます。  
確認済みの対象を非対話で処理するときは、`--apply --yes` を指定してください。  
削除は直接行い、元に戻すには再取得・再ビルドが必要です。

## 開発

パッケージはリポジトリ直下の `mac_junk_cleaner/` に配置しています。  
`uv sync` で Ruff・Pyright・taskipy も導入されます。

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
