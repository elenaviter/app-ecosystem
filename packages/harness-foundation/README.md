# harness-foundation

A shared harness contract for reactive agents at scale.

## Current Status

`0.0.1` is an installable planning marker that reserves the distribution and
import names. It currently exposes only `harness_foundation.__version__`; the
production harness has not yet been extracted into this package.

```bash
python -m pip install harness-foundation
```

## Intended Boundary

The package will own the machinery around an agent turn:

- ordered, durable delivery per conversation;
- mid-run follow-up, steer, and stop;
- distributed turn lifecycle and duplicate-run prevention;
- durable conversation records and governed workspaces;
- one hosting contract for the native ReAct Agent and agents that keep their
  own loop, such as LangGraph- or Claude Code-based agents.

It will not own application behavior or delegated authority policy. Connection Hub
owns authority; [`app-foundation`](https://github.com/elenaviter/app-ecosystem/blob/main/packages/app-foundation/README.md) owns generic
host capabilities.

The running implementation remains in
[KDCube](https://github.com/kdcube/kdcube). Extraction will preserve the
existing framework-specific loops and add the shared harness around them.

License: MIT. Source: https://github.com/elenaviter/app-ecosystem
