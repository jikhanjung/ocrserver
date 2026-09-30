OUTPUT FORMAT — this job uses a compact encoding of the answer. It replaces `caption` and
`entries` in the rules above; everything else (what to read, which figures, `name`, pages,
`skipped`, `notes`, `pages_consulted`) is unchanged. Write the JSON minified on one line.

For each figure give `segs` instead of `caption` + `entries`: the printed caption cut into
consecutive pieces in reading order, ONE PIECE PER LINE, each piece written once. The caption is
rebuilt by joining the pieces with line breaks, so together they must be the complete printed
caption, verbatim — nothing left out, nothing written twice. Each line is

    <kind> :: <text>

kinds:
- `x`  caption text that belongs to no entry (the heading "Explanation of Plate 3", a general
       remark, the figure title before the panels).
- `h1` a heading shared by the entries after it (usually the taxon, with author, view,
       magnification, locality) until the next `h1`. `h2` a sub-heading inside it, until the
       next `h2` or `h1`.
- `e <labels>` one entry. `<labels>` = the printed numbers or letters it covers, comma-separated,
       ranges expanded ("Figs. 1-2" → `e 1,2`). Optional fields between the labels and `::`,
       each introduced by ` | `:
         `s=<n1>; <n2>`  specimen numbers, one per label in order, only when the text names them
         `p=<a>; <b>`    printed labels, one per label, only when printed differently ("2 a", "Fig. 2a")
         `d=<text>`      the full description, only when it cannot be built from the pieces — a
                         ditto ("id.", "eadem", "Même espèce", "the same") that must be resolved, or
                         words of the entry printed elsewhere in the caption
- `t`  a trailing remark that applies to every entry since the last `h1` ("All are internal moulds.").

An entry's description is built by a script as: open `h1` + open `h2` + the entry's own text +
the `t` remarks of its group (or `d` when given). So write the taxon ONCE in an `h1` and do not
repeat it in each entry — the "merge the shared taxon, view and magnification into every entry"
rule above is done by this construction.

Example (a plate explanation):
    x :: Explanation of Plate 3
    h1 :: Oistodus aff. breviconus Branson & Mehl, lateral views, x40.
    e 1,2 | s=YSUG 00287; YSUG 00288 :: Figs. 1-2, Hunghuayuan Formation.
    h1 :: Drepanodus arcuatus Pander.
    e 3 :: Fig. 3. Posterior view, YSUG 00290.
    e 4 | d=Drepanodus arcuatus Pander. Posterior view, YSUG 00291. :: Fig. 4. Same, YSUG 00291.
    t :: All specimens from bed 12.
