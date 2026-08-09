# Doc-writing reference: before/after pairs

Each pair shows a defensive draft, the rewrite, and the diagnosis. The patterns matter more than the domains.

## 1. CLI flag behavior

**Before**

> When the `--force` flag is provided, the command will, after prompting the operator for confirmation and receiving an affirmative response, proceed to overwrite the existing configuration file. It should be noted that entries not in conflict are preserved during this process. This operation does not validate the resulting file against the schema, nor does it create a backup unless `--backup` is also passed; `--backup` is supported only on POSIX systems, a limitation that should be taken into account before relying on this workflow.

**After**

> `--force` overwrites the existing config after a confirmation prompt. Pass `--backup` (POSIX only) to keep a copy of the old file.

**Diagnosis** — "will proceed to" and "it should be noted that" add zero information. "Entries not in conflict are preserved" is what overwrite-with-merge means. The disclaimer about not validating is a non-event unless the tool elsewhere validates. Two clauses carry all the facts.

## 2. Prerequisites

**Before**

> Before proceeding to the installation step, the operator must ensure that the runtime environment satisfies the prerequisite of Python version 3.11 or higher. While earlier versions may appear to function for a subset of commands, they do not constitute a supported configuration, and no guarantee is made, express or implied, regarding the behavior of the software therein; users encountering issues on unsupported versions are advised that such issues may not be treated as defects.

**After**

> Requires Python ≥ 3.11.

**Diagnosis** — The entire second sentence exists to preempt a bug report, not to help an installer. "Requires" already means everything the paragraph strains to say.

## 3. Non-events and hedged conclusions

**Before**

> During this setup, a temporary keypair is generated and subsequently discarded upon completion; this intermediate event is reported here for the sake of procedural completeness, although it has no bearing on the final credential. Overall, the observations support the relatively cautious conclusion that, under the tested conditions, the onboarding flow tends to produce a working credential.

**After**

> *(Delete the whole passage.)*

**Diagnosis** — A discarded temporary is a non-event; if persistence mattered, the doc should state what *is* persisted, once. A setup guide must state outcomes ("the flow writes the credential to `~/.app/credentials.json`"), not review its own confidence. If the outcome is genuinely uncertain, name the failure mode and the check that resolves it — that is actionable, hedging is not.

## 4. One fact, three homes

**Before** — the same sentence, pasted into the README, the install guide, and the command reference:

> These commands operate only the authorization control plane; they do not start the server, do not begin message polling, and require the configured platform to match the command platform.

**After** — the command reference keeps the full statement (it owns command behavior):

> Every command loads the config first and requires its `platform` to match the command platform. These commands do not start the server or message polling.

The install guide keeps a plain pointer:

> Command/platform mismatch behavior is covered in [Commands](docs/COMMANDS.md).

The README drops it entirely; a quick-start reader cannot act on it, and the CLI explains itself interactively when a mismatch occurs.

**Diagnosis** — Repetition across documents is not thoroughness; it is three future contradictions waiting for the next behavior change. Ask who can act on the fact, at which step, and let that document own it.

## 5. 中文示例：操作步骤

**Before**

> 完成上述配置步骤之后，操作人需要把 bootstrap 配置中的空数组替换为包含真实身份信息的配置项；需要特别指出的是，下方示例中给出的取值仅仅起到指示填写位置的作用，不构成可以直接复制使用的有效取值，操作人应当根据自己实际的注册结果，在保留数组中已有条目的前提下，将返回的身份标识合并进去。

**After**

> 完成后白名单应包含返回的身份：
>
> ```toml
> allowed_users = ["<注册流程返回的 open_id>"]
> ```

**Diagnosis** — 角括号占位符本身已经说明"不能原样复制"；"保留已有条目"是合并的默认含义；"需要特别指出的是"是审计腔。中文文档同样适用：默认路径一句话，例外一个短句，占位符交给符号自己解释。
