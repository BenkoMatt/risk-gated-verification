# Reference plugin

This directory is the Hermes-specific implementation. The pure logic is tested
from the repository root:

```bash
python -m unittest discover -s tests -v
```

## Install

```bash
mkdir -p ~/.hermes/plugins
cp -R plugin ~/.hermes/plugins/risk-gated-verification
```

Add [`config.example.yaml`](config.example.yaml) to the target profile's
`config.yaml`, then:

```bash
hermes plugins doctor risk-gated-verification
hermes plugins enable risk-gated-verification
```

Start a new chat before judging the behavior. Existing sessions can retain the
old prompt/tool wiring.

## Environment overrides

| Variable | Default | Purpose |
|---|---:|---|
| `RISK_GATE_VERIFIER_MODEL` | `inkling-2-small` | Independent verifier model |
| `RISK_GATE_VERIFIER_TIMEOUT` | `28` | Verifier timeout in seconds |
| `RISK_GATE_VERIFIER_MAX_TOKENS` | `1400` | Verifier output cap |
| `CLEF_MODEL` | `clef:27b` | Typed decision model |
| `CLEF_ENDPOINT` | `https://ollama.com/v1/systemone` | Decision-model endpoint |
| `CLEF_TIMEOUT` | `3.0` | Decision-model timeout |
| `RISK_GATE_DEBUG` | off | Write compact JSONL traces to `$HERMES_HOME/logs/risk-gate-debug.log` |

`OLLAMA_API_KEY` is read from the environment, with a fallback to
`$HERMES_HOME/.env`. No key is stored in this repository.

## Files

- `__init__.py` — classifier, optional decision-model adapter, evidence trail,
  independent review, quote sanitizer, and four hook handlers.
- `plugin.yaml` — Hermes manifest and config schema.
- `config.example.yaml` — a complete profile config block.

## Scope

This reference intentionally omits the delegated verifier subagent. It was
tested and failed its JSON contract; see
[`../docs/evaluation-notes.md`](../docs/evaluation-notes.md). Direct review is
the default and the reliable path.
