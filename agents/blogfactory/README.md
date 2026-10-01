# Blogfactory

Blogfactory は、量産時に入り込みやすい偏りを、人が公開前に見抜けるようにするブログ制作工程です。記事を自動公開せず、根拠・偏りの兆候・設定をレビュー可能な形で残します。

安全の下限 A は全テナント共通で変更できません。来歴のない経験や高リスク主張、テナントをまたぐソース、自動公開を許しません。設定 B は人称の既定、調査、公開先、承認粒度、レビュー指標などで、中立の既定値からキャンペーンごとに選べます。B は A を緩めません。

```
resolve → run [generate → provenance → audit → review] → 人の承認 → publish
```

`run` は `REVIEW_READY` で止まり、公開は人の承認 artifact を添えた明示的な `publish` だけです。中立は品質や中立性の保証を意味しません。外販時も「設定とレビューの可視性」を説明し、内容が中立であると保証する表現は使いません。

## 既存 Blog Agent との接続

Blogfactory は一記事の本文生成を既存 `agents/blog/` に委ねます。`make_blog_agent_generator` は承認済み・同一テナント・指定人称を許可したソースだけを入力に集め、Blog Agent の宛先と指示へ plan の題・主張・記事型・人称を渡します。生成物の来歴を保証するものではなく、必ず runner が後段の provenance gate にかけます。

```python
from blogfactory.generators import make_blog_agent_generator
from blogfactory.runner import run_campaign

generator = make_blog_agent_generator(registry=registry)  # 既定は local file source + Blog Agent/LLM
run_campaign(campaign_dir="campaign", generator=generator,
             resolved_policy=effective_policy, registry=registry)
```

`build_fn(source_text, directives)` と `source_loader(source_record)` は注入できます。テストや独自のLLM/ストレージでは注入し、既定の `build_fn` は既存 Blog Agent の `blog_core.generate()` を遅延呼び出しします。バックエンドは effective policy の `llm_backend`、なければ `STUDIO_LLM_BACKEND`、さらに未指定なら `codex` です。既定の source loader は `file:` URI だけを読み、ネットワーク URI には明示的な loader が必要です。

## CLI

リポジトリ直下から、まずポリシーを解決します。

```bash
python studio-agents/agents/blogfactory/cli.py resolve campaign --neutral studio-agents/agents/blogfactory/policy/neutral-defaults.yaml --tenant-id tenant-a
python studio-agents/agents/blogfactory/cli.py run campaign --stub
python studio-agents/agents/blogfactory/cli.py status campaign
```

CLI の `run` は安全な `--stub` 専用です。実生成は上のようにアプリケーション側で generator を注入し、review 後に人が承認してから `publish` を呼びます。
