# Epoch report is Markdown alone

| Measurement | Delta |
|---|---:|
| Total | -4,671 |
| Production | -2,806 |
| Production logic | -2,023 |

The analyzer stops writing `analysis.html` and `analysis.fragment.html` and
stops drawing SVG figures, so `analysis.md` is the one published form of the
epoch report and the console's Publication view renders it with the Markdown
renderer the console already had. The removed production code is the
`analyzer/svg/` package, `analyzer/report_figures.py`, and the Markdown-to-HTML
converter, embedded stylesheet and HTML writers in `analyzer/report.py`,
together with the route, reader and layout path that served the HTML. Each
view a figure carried is now a table in the report, built by a few short
functions in `analyzer/report_sections.py`. The removed test lines covered the
HTML and SVG output; a new test file covers the tables that replace the
figures.
