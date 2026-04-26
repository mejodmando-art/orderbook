# OpenRouter Free Models

Verified available as of 2025-04-26. Check live availability:

```bash
curl https://openrouter.ai/api/v1/models | python3 -c "
import json,sys
data=json.load(sys.stdin)
free=[m['id'] for m in data['data'] if ':free' in m.get('id','')]
print('\n'.join(sorted(free)))
"
```

## Verified Available (2025-04-26)

```
google/gemma-3-4b-it:free
google/gemma-3-12b-it:free
google/gemma-3-27b-it:free
google/gemma-4-26b-a4b-it:free
google/gemma-4-31b-it:free
meta-llama/llama-3.2-3b-instruct:free
meta-llama/llama-3.3-70b-instruct:free
nousresearch/hermes-3-llama-3.1-405b:free
nvidia/nemotron-3-nano-30b-a3b:free
nvidia/nemotron-3-super-120b-a12b:free
openai/gpt-oss-20b:free
openai/gpt-oss-120b:free
qwen/qwen3-coder:free
qwen/qwen3-next-80b-a3b-instruct:free
```

## Reliability Notes

| Model | Provider | JSON reliability | Rate limit | Notes |
|---|---|---|---|---|
| `meta-llama/llama-3.3-70b-instruct:free` | Meta | ✅ High | ⚠️ 429 common | Most popular → hits limits fast |
| `google/gemma-3-12b-it:free` | Google | ✅ High | ✅ Generous | Use 12B not 27B — 27B returns 400 on long prompts |
| `google/gemma-3-27b-it:free` | Google | ✅ High | ✅ Generous | ⚠️ 400 if prompt > ~2000 chars |
| `openai/gpt-oss-20b:free` | OpenAI OSS | ✅ High | ✅ Generous | Good for structured JSON |
| `openai/gpt-oss-120b:free` | OpenAI OSS | ✅ Very high | ✅ Generous | Best judge model |
| `nousresearch/hermes-3-llama-3.1-405b:free` | NousResearch | ✅ High | ⚠️ 429 common | Large model, popular |
| `nvidia/nemotron-3-super-120b-a12b:free` | NVIDIA | ❌ Unreliable | ✅ OK | Returns empty responses frequently |
| `qwen/qwen3-235b-a22b:free` | Alibaba | ❌ 404 | — | Model ID changed/removed |

## Recommended Combinations

### Analyst trio (spread across providers)
```python
ANALYST_MODELS = [
    ("LLaMA-70B",  "meta-llama/llama-3.3-70b-instruct:free"),
    ("Gemma-12B",  "google/gemma-3-12b-it:free"),
    ("GPT-OSS-20B","openai/gpt-oss-20b:free"),
]
```

### Judge
```python
JUDGE_MODEL = "openai/gpt-oss-120b:free"
```

## Common Errors and Fixes

| Error | Cause | Fix |
|---|---|---|
| `HTTP 404` | Model ID wrong or removed | Check live list, update model ID |
| `HTTP 400` | Prompt too long for model | Reduce to <2000 chars, use smaller model |
| `HTTP 429` | Rate limit hit | Add retry with backoff, use different provider |
| Empty content | Model returned blank | Retry once, or switch model |
| `Expecting value: line 1 column 1` | Empty/HTML response | Model overloaded — retry or switch |

## Prompt Size Limits (approximate)

| Model | Safe prompt size |
|---|---|
| Gemma-12B | ~3000 chars |
| Gemma-27B | ~2000 chars |
| LLaMA-70B | ~8000 chars |
| GPT-OSS-* | ~8000 chars |
| Hermes-405B | ~8000 chars |
