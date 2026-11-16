"""Isolated helper: list Apple's full-installer products via gibMacOS's parser.

Run as a subprocess (never imported) so gibMacOS's screen-clears and its
multiprocessing catalog downloader stay fully contained — this process's stdout
and stderr are discarded by the caller. We write the parsed product list, as
JSON, to the path given in argv[3]:

    python3 -I giblist.py <gibMacOS_dir> <catalog> <out.json>

Each item: {"title", "version", "build", "url"} for products that ship an
InstallAssistant.pkg (the modern full installers slickstick can build from).
"""
import json
import sys


def main() -> int:
    gib, catalog, out = sys.argv[1], sys.argv[2], sys.argv[3]
    sys.path.insert(0, gib)
    import gibMacOS  # noqa: E402  (located via the path above)

    g = gibMacOS.gibMacOS()
    if catalog:
        g.current_catalog = catalog
    g.get_catalog_data()
    prods = g.get_dict_for_prods(g.get_installers())

    items = []
    for p in prods:
        url = next((x["URL"] for x in p.get("packages", [])
                    if "InstallAssistant.pkg" in x.get("URL", "")), None)
        if not url:
            continue
        items.append({
            "title": p.get("title", ""),
            "version": p.get("version", ""),
            "build": p.get("build", ""),
            "url": url,
        })
    with open(out, "w") as fh:
        json.dump(items, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
