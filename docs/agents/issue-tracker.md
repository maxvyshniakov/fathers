# Issue tracker

- GitHub Issues of `maxvyshniakov/fathers`, in English. Pass the repository every time:
  `gh issue list -R maxvyshniakov/fathers`, `gh issue view <n> -R maxvyshniakov/fathers --comments`,
  `gh issue create -R maxvyshniakov/fathers --title ... --body-file ... --label ...`,
  `gh issue comment <n> -R maxvyshniakov/fathers --body-file ...`, `gh issue close <n> -R maxvyshniakov/fathers`.
- Labels: `pkg:slotfather`, `pkg:lanefather`, `pkg:junkfather`, `pkg:godfather` for the package; `bug`,
  `enhancement`, `documentation`, `accessibility` for the kind. Cross-package tickets carry no `pkg:` label.
- States: in progress is a claim comment naming the lane branch; `question` means waiting on the owner; blocked is a
  `Blocked by #N` line in the body (or `**Order.** After #N`); `integrated` means landed on the integration branch,
  not yet on `main` (Lanefather sets it, [orchestration](orchestration.md)).
- Titles: `<package>: <the outcome>` in lower case (`junkfather: typed entries and Pyright strict`); `fathers:` or
  the area (`CI:`) for the repository. Bodies: `**Problem.**`, `**Change.**`, `**Done when**`, and `**Order.**` when
  another ticket comes first; link other tickets as `maxvyshniakov/fathers#N`.
- Reconciliation: a commit or landing merge that completes a ticket's whole scope ends with `Fixes #N`, so GitHub
  closes it when it reaches `main`; partial work says `Refs #N`. Close by hand only completed scope, with the SHA on
  `main` in a comment. Follow-ups go on the ticket as checklist items or new linked tickets.
