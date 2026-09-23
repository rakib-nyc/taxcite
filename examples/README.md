# Examples

| File | What it is |
|---|---|
| `sample_memo.md` | A memo with deliberately planted citation and quotation errors |
| `sample_report.md` | What `taxcite verify examples/sample_memo.md` prints for it, against a full Title 26 index at release point 119-110 |
| `claude_desktop_config.json` | Claude Desktop MCP server configuration |
| `github-workflow.yml` | A workflow that runs TaxCite on changed Markdown in a pull request |

`sample_report.md` is generated, not written by hand. Regenerate it with:

```bash
uv run taxcite verify examples/sample_memo.md --fail-on never > examples/sample_report.md
```

The suggestions in it depend on how much of Title 26 is indexed, so the copy checked
byte-for-byte by the test suite lives at `tests/fixtures/snapshots/sample_report.md`
and is rendered against the small fixture index instead.
