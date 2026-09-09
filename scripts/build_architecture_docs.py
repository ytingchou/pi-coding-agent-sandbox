"""Build the offline architecture handbook from repository-owned sources."""

import argparse
import html
import json
import re
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/architecture.html"
SOURCES = [
    "docs/architecture/README.md",
    "docs/architecture/deployment.md",
    "docs/architecture/runtime.md",
    "docs/architecture/security.md",
    "docs/architecture/decisions.md",
    "docs/mongodb-schema.md",
    "docs/mongodb-registry-architecture.md",
    "docs/resources.md",
    "docs/python-packages.md",
    "docs/session-lifecycle.md",
    "docs/session-verification.md",
    "docs/configuration.md",
    "docs/mongodb.md",
    "docs/kubernetes.md",
    "docs/development.md",
    "charts/pi-sandbox/README.md",
]


def build():
    ids = {(ROOT / path).resolve(): f"chapter-{i}" for i, path in enumerate(SOURCES)}
    nav, chapters = [], []
    markdown = MarkdownIt("commonmark", {"html": False}).enable("table")
    for source in SOURCES:
        path = ROOT / source
        content = path.read_text()
        title = content.splitlines()[0].removeprefix("# ")
        chapter_id = ids[path.resolve()]
        tokens = markdown.parse(content)
        for token in tokens:
            for child in token.children or []:
                if child.type != "link_open":
                    continue
                href = child.attrGet("href") or ""
                if not href or ":" in href or href.startswith("#"):
                    continue
                target = (path.parent / href.split("#")[0]).resolve()
                if not target.is_relative_to(ROOT) or (target != OUTPUT and not target.exists()):
                    raise ValueError(f"Broken or out-of-repository link in {source}: {href}")
                child.attrSet(
                    "href",
                    "#overview"
                    if target == OUTPUT
                    else "#" + ids[target]
                    if target in ids
                    else "../" + target.relative_to(ROOT).as_posix(),
                )
        rendered = markdown.renderer.render(tokens, markdown.options, {})
        rendered = re.sub(
            r'(<pre><code class="language-mermaid">.*?</code></pre>)',
            r'<details class="source-diagram"><summary>Mermaid 圖原始碼'
            r" (互動總覽見頁首)</summary>\1</details>",
            rendered,
            flags=re.S,
        )
        rendered = rendered.replace("<table>", '<div class="table-wrap"><table>').replace(
            "</table>", "</table></div>"
        )
        chapters.append(
            f'<article class="doc" id="{chapter_id}" aria-label="{html.escape(title)}">'
            f'<div class="source">SOURCE / {html.escape(source)}</div>{rendered}</article>'
        )
        nav.append(f'<a href="#{chapter_id}">{html.escape(title)}</a>')
    topology = json.loads((ROOT / "docs/architecture/topology.json").read_text())
    for name in ("compose", "k8s", "session"):
        graph = topology[name]
        node_ids = {node["id"] for node in graph["nodes"]}
        if len(node_ids) != len(graph["nodes"]):
            raise ValueError(f"Duplicate nodes in {name}")
        for edge in graph["edges"]:
            if edge["source"] not in node_ids or edge["target"] not in node_ids:
                raise ValueError(f"Missing edge endpoint in {name}")
    # Prevent JSON data from terminating its script element if documentation changes.
    payload = json.dumps(topology, ensure_ascii=False).replace("<", "\\u003c")
    template = (ROOT / "docs/architecture/template.html").read_text()
    result = template.replace("@@NAV@@", "\n".join(nav))
    result = result.replace("@@CHAPTERS@@", "\n".join(chapters))
    return result.replace("@@TOPOLOGY@@", payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if HTML needs rebuilding")
    args = parser.parse_args()
    result = build()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != result:
            raise SystemExit("Architecture HTML is stale; run make docs")
        print("Architecture HTML matches Markdown, topology and template sources")
    else:
        OUTPUT.write_text(result)
        print(f"Built {OUTPUT.relative_to(ROOT)} ({len(result.encode()):,} bytes)")


if __name__ == "__main__":
    main()
