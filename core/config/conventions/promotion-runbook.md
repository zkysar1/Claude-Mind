# Promotion Runbook — Frontier → Seed → Downstream

Operator procedure for promoting a framework release one or two hops down the
chain. The CHAIN itself (which repo is frontier/seed/downstream, who signs off,
what omni may not do) is `.claude/rules/promotion-cycle.md`; plant blast-radius
classes are the `seed-plant-living-prod-safety` tree node. THIS file is the
step-by-step run: what to execute, what each gate's output means, and — the
part that used to be re-derived by hand every run — how to triage a
DO-NOT-PROMOTE plan verdict mechanically.

Distilled from the 2026-07-30 → 2026-08-01 v2.8.7→v2.8.10 arc (four promote
runs, two forced verdicts, 126 flagged files triaged to zero unexplained).

## Phase 0 — Preconditions at the frontier

1. Working tree clean. Fleet agents dirty shared ledgers continuously — commit
   theirs as a `chore:` (never discard), then `git fetch && git merge && git push`
   until 0/0 vs origin. Expect to repeat: the fleet races you.
2. Read the LAST promotion's force ledger / receipts if any exist (scratchpad
   or commit messages). The prior run's dest-only analysis is this run's
   baseline for "what changed since".
3. Target-clone freshness, and the disjointness proof that makes it safe.
   The clone you are about to plant into is dirtied continuously by the agents
   living in it, so neither "just `git pull`" nor a blanket dirty-tree refusal
   is right: what makes a fast-forward safe is that the DIRTY set and the
   INCOMING set do not intersect. One command (guard-399):

   ```bash
   bash core/scripts/promotion-git-state.sh freshness --target <target-clone> [--apply]
   ```

   `SAFE` (rc 0) — disjoint; `--apply` fast-forwards. `UNSAFE` (rc 2) — it
   names the colliding paths; commit those as a `chore:` (never discard) and
   re-run. `DIVERGED` (rc 2) — the target is ahead as well as behind, which is
   a Phase 1 reconcile-UP question, not a freshness one. `UNREADABLE` (rc 3) —
   no verdict is available; a wrong `--target` must never read as clean.

## Phase 1 — Reconcile UP before anything ships (guard-119)

Promotion is a RECONCILE, not a mirror. Run
`promotion-preflight.sh --source <frontier> --target <dest-clone>` and treat
exit 2 as a work list, not a blocker override prompt:

- For each target-ahead file, read the DEST's authoring commits (`git log -p`
  at dest) — the diff IS the back-port spec.
- **Semantic carry, not byte carry.** A dest fix that hardcodes ITS
  deployment's values (aspiration IDs, box paths, agent rosters) must be
  GENERALIZED on the way up: the framework file carries the resolution
  protocol; the concrete per-deployment values go to a world-overlay
  convention that each deployment owns a copy of (pattern:
  `world/conventions/deployment-routing.md`, g-001-195). The
  domain-leak-check is the enforcement backstop — deployment names in a base
  skill are a leak.
- Parallel evolution is common: dest and frontier often fix the same defect
  independently. When the frontier's form is equal-or-better, carry NOTHING —
  record the finding in the force ledger instead.

## Phase 2 — Cut the release

`release.sh {patch|minor|major} --summary "..."` then
`git push origin main --tags`. release.sh is the sole tag creator and never
pushes.

## Phase 3 — Hop: promote one step down

**Worktree-at-tag method (required on an active frontier).** The fleet moves
HEAD between your tag and your promote run, and the promote refuses
HEAD≠tag. Do not chase HEAD — pin it:

```bash
git worktree add <scratch>/wt-vX.Y.Z vX.Y.Z
cp agents/<agent>/local-paths.conf <scratch>/wt-vX.Y.Z/agents/<agent>/
cp .env.local <scratch>/wt-vX.Y.Z/
```

Both copies are load-bearing: they are gitignored, so the worktree lacks
them, and without them role/world resolution fails with misleading errors
(observed shape: an MSYS path-translated "cannot resolve promotion roles:
.../Git/config/..."). Remove the worktree after the run
(`git worktree remove <path>`).

Then from inside the worktree:

```bash
bash core/scripts/promote-to-upstream.sh --target <dest-clone> \
     --branch "promote/vX.Y.Z" --pr
```

Merge the PR yourself once mergeable (guard-680 grant); sync the dest clone.

**Then TAG the dest merge commit and push the tag** — this is the step nothing
else performs, and without it the pull side is blind:

```bash
git -C <dest-clone> tag -a vX.Y.Z -m "vX.Y.Z — plant of <source-repo> vX.Y.Z (source sha <sha>)" <merge-sha>
git -C <dest-clone> push origin vX.Y.Z
```

`pull-promotion.md` C3 has every downstream Mind discover a release by listing
the STAGING clone's tags, and neither the plant nor the promote PR creates one
(`release.sh` tags only the source). Measured 2026-08-27: staging carried v2.12.3
content while its newest tag was still **v2.9.4**, and a downstream clone's
`git describe` read `v2.9.4-16-gdd5e281` — three plants that no poll could see.
The dest's `pre-push` M2 gate (`check-tag-in-releases.py`) requires the version
in `RELEASES.json`, which the plant delivers, so the push passes without an
override; if it refuses, the plant did not land `RELEASES.json` and THAT is the
defect to fix, not the gate.

**The dest clone's own checked-out branch is never touched** (g-115-4803). Under
`--pr` the promote plants into an isolated worktree of the dest — created before
the plan, torn down after the push — so the destination's live checkout stays on
whatever branch it was on, on every path including a failed plant, a failed
verify, and a crash. It used to `git checkout -b` in the dest itself and leave it
there, which silently repointed a working deployment at an unmerged branch. So
"sync the dest clone" above now means pull the merged main, not un-switch a
branch. If a run dies before teardown, the leftover worktree is removed with
`bash core/scripts/worktree-teardown.sh <path> --owner <dest-clone> --force`;
`--owner` is required whenever the directory is already gone, because otherwise
the owning repo is derived FROM that directory.

Note the two worktrees are different and both are load-bearing: the SOURCE-side
one above pins the tag so the payload cannot drift mid-run (guard-678), and this
DEST-side one keeps the promotion out of a live deployment's checkout. The
source-side worktree still needs its gitignored files copied in (`local-paths.conf`,
`.env.local`); the dest-side one deliberately does not, and must not be given
them — the plant's living-prod protections read `None` from an unlocatable dest
registry and fail SAFE toward preserving every skill dir, whereas the living-prod
DETECTION runs against the real clone precisely because `.mind-data` is gitignored
and absent from any worktree.

**`SOURCE DRIFTED MID-PROMOTION` — what it means and what to do** (g-115-3514).
The promote asserts clean-tree + `HEAD == vX.Y.Z` twice: once at Step 1, and
again immediately before the plant. The second check exists because the plant
copies from the WORKING TREE and a full-chain run takes 15+ minutes, so anything
landing in that window used to ship downstream wearing the old tag's label —
measured 2026-07-27, when two mid-run commits shipped as v2.7.1, leaving the
downstream payload for that tag carrying code the tag does not contain.

Note how it relates to the dirty-tree refusal you already know: that check is
strictly weaker on its own, because it catches UNCOMMITTED changes and is blind
to changes COMMITTED after the tag (which leave the tree perfectly clean). The
two conditions are only a guarantee TOGETHER — clean tree AND HEAD==tag means the
working tree's content IS the tag's content, which is what makes copying from the
working tree equivalent to planting from the tag.

There is deliberately **no override**. Every other gate here has one
(`--force-past-plan`, `PROMOTE_ALLOW_DRIFT`, `--skip-preflight`); this one must
not, because bypassing it means knowingly shipping untagged content under a tag.
Two honest fixes: re-run from a worktree pinned at the tag (the method above,
which makes the re-check a guaranteed no-op — a detached worktree cannot drift
while the fleet commits to `main`), or cut a new release with `release.sh` so the
label matches what you are actually shipping.

## Phase 4 — Plan-verdict triage (the formerly-hand-derived part)

A DO-NOT-PROMOTE verdict (exit 21) means the dest carries lines the
transformed seed lacks. That is ONE observation with FOUR causes, and only
one of them should stop a promotion:

| Class | Meaning | Test | Action |
|---|---|---|---|
| **DEST-FROZEN** | dest has had zero commits since the last plant — nothing there can be authored | dest HEAD == last promote-PR merge AND tree clean AND 0 commits since | force; repo-level proof covers EVERY flag |
| **SEED-MOTION** | dest file is untouched since the prior plant; the frontier moved | dest file byte-equal to transform(prior-tag:file) | force; per-file proof |
| **SYNC-VINTAGE** | file's dest history is only sync/plant commits | dest last-writer commit is a framework-sync commit | force; corroborates seed-motion |
| **AUTHORED** | a resident agent wrote it | none of the above | STOP — Phase 1 forensics; back-port UP, then re-run |

`core/scripts/promotion-plan-triage.py` automates the first three tests and
emits the evidence ledger; only AUTHORED residue needs a human/agent read.
Run it whenever the plan blocks:

```bash
py -3 core/scripts/promotion-plan-triage.py \
   --source <seed-repo> --target <dest-clone> --prior-tag vX.Y.(Z-1) \
   --plan-log <promote-run-log>
```

**Force discipline.** `--force-past-plan` takes a WRITTEN justification.
Write the full decomposition ledger, not a vibe: every flagged file assigned
a class, with the proof named (repo-frozen / byte-equal / last-writer /
back-ported-in-commit-X / superseded-by-Y). The two 2026-08-01 forces (18/18
and 108/108 decomposed) are the reference shape. An unexplained residue means
DO NOT force.

**Known structural fact:** a staging repo with no resident agents can only
ever produce DEST-FROZEN or SEED-MOTION flags, yet the plan will still block
every run whose release modified previously-planted files — i.e., nearly all
of them. Until the auto-excusal lands inside the plan itself, expect one
mechanical force per staging hop.

## Phase 5 — Post-plant verification at a living dest

Run every item; each is a one-liner and each has caught a real defect:

1. `__version__` at dest == the promoted tag.
2. Resident agent's forged skills survive: registry entry count + skill-dir
   count unchanged.
3. Dest-local preserved blocks survive: grep for each block the dest's agent
   has marked KEEP-IN-SYNC (and when the block was back-ported up, confirm
   the plant delivered the upstream copy WITH its provenance note rather
   than deleting it).
4. `.gitignore` intact (secrets globs).
5. TZ posture (`TZ=UTC` in settings env).
6. Notification chain resolves: the notify skill's `companion_scripts` all
   exist at dest.
7. If the release introduced a framework file that resolves through a
   world-overlay convention, CREATE the dest world's copy of that overlay —
   the plant never writes world/ data.
8. Full suite at the dest box (`run-full-suite.sh`), judged by FAILING FILE
   SET against the dest's own baseline, never by count:
   - newly-COLLECTED tests (testpath/runner changes) are visibility, not
     regression;
   - fixtures assuming the frontier's domain/roster are perma-red at dest
     (known class);
   - test literals the de-brand transform rewrote while the code derives the
     value from data are upstream test bugs that only manifest at dest
     (known class);
   - a plant that flips prior baseline reds GREEN is evidence in its favor —
     name them.
9. Transform-damage scan — did the transform DELETE something it should not
   have? This is the mirror of Phase 4: that phase reads lines the DEST carries
   and the seed lacks; this reads the other direction.

   ```bash
   py -3 core/scripts/seed-damage-scan.py --source <seed-repo> --target <dest-clone>
   ```

   Items 2 and 3 above already check two damage classes by hand (forged skills
   survive, KEEP-IN-SYNC blocks survive). This covers the third: the strip
   vocabulary the manifest's `word_list_strip` rules act on, which no hand
   one-liner can enumerate.

   Read the exit code, not the count. `0` clean, `2` sites found, **`3` means
   the scan could not address anything and NO verdict is available** — a wrong
   `--target` root otherwise produces a clean zero indistinguishable from a
   healthy hop (guard-1587), so the tool refuses rather than reassuring. Every
   verdict prints its denominator; a zero without one is not evidence.

   A reported site is not automatically a defect: the transform working as
   designed lands here too — both survivors of the measured 4,829 -> 2
   reduction were exactly that (rb-6267). Read each site.

## Phase 6 — Handoff

1. Post a handoff to the dest world's coordination board
   (`peer-board-post.sh --peer <env-id> --channel coordination --type handoff`;
   needs `PEER_WORLD_<ENV_ID>` or `peer_world_path:` on this box). Say what
   traveled, what was generalized where, what suite reds are pre-classified,
   and anything the resident agent should adjust in its world overlays. If
   the dest world is git-tracked, commit + push the board file — a post that
   only lands in your local clone reached nobody.
2. Box updates while the resident agent is stopped (runtime upgrades, CLI
   updates, repo fast-forward) — this is the window.
3. The USER restarts the resident agents. Not the promoting agent's call.
4. First post-restart brief from the resident agent is part of the promotion:
   read it, verify anything it attributes to the plant (last-writer +
   introduction-commit checks distinguish "plant caused it" from "plant
   surfaced it"), and back-port any fix it made that lives in framework
   files. A latent upstream defect that only manifests on the dest's
   platform (exec-bit class, path semantics, CRLF) is the expected shape
   here — platform is part of production shape.

## Phase 7 — Postflight: the git state the hop leaves behind

`promote-to-upstream.sh` creates and pushes a `promote/*` branch and tears down
its own worktree. Nothing deletes the branch, confirms the merge actually
landed, or resolves a pre-promotion stash — so those survive the run, silently,
and two promotion stashes were measured stranded on dev and prod before this
phase existed.

One command, not a checklist (guard-399 — a hook whose invocation is prose does
not fire):

```bash
bash core/scripts/promotion-git-state.sh postflight \
  --target <target-clone> --branch promote/vX.Y.Z --pr <url|number> \
  --tag vX.Y.Z [--plant-clone <path>] [--also-confirm <repo>]...
```

Read-only by default; `--apply` performs the branch deletion and nothing else.
Exit `0` clean · `2` action needed · `3` unreadable.

**(a) What the script does or checks for you.** Merge-landed verdict, the
push-triggered main run, branch deletion local + remote, a stale-worktree scan,
tag reachability, and a final fetch-and-confirm across every repo you name.

Three of those are counter-intuitive enough to state, because a naive check
inverts each one:

- **The merge verdict is the PR's `state`, never ancestry.** A squash merge
  lands the content under a NEW sha, so the branch tip can never become an
  ancestor of main — not "not yet", permanently (guard-4463). The script also
  never trusts a local `remotes/origin/*` ref: `git fetch` does not prune
  deleted branches, so a stale ref keeps an orphaned tip alive and an
  "unlanded commit" scan reports commits that shipped weeks ago. Every fetch
  here is `--prune`.
- **The main workflow run is matched by `headSha`, never by recency**
  (guard-5017). `gh pr checks` green proves the PR gate only; the merge commit
  is a new commit with its own run, and main-only jobs (publish, deploy legs)
  exist ONLY there. Right after a merge that run is queued and unlisted, so
  `--limit 1` grabs the PREVIOUS push's run — already complete, reading as an
  instant green for a commit it never touched. `NO_RUN_YET` is a real state
  and is NOT a green.
- **Branch deletion is gated on `MERGED`, and refuses without it.** Going
  forward also pass `--delete-branch` at `gh pr merge` time — prevention beats
  sweeping — but then fetch and ff explicitly afterwards: gh switches and
  fast-forwards the LOCAL checkout, and a diverged local leaves STALE
  working-tree content behind (rb-240).

**(b) What the script only DETECTS — you still have to do these.** They are
printed under their own `LLM OBLIGATIONS` heading precisely so they cannot be
read as done (guard-365):

- **A pre-promotion stash: apply or archive, NEVER silent-drop.**
  `git stash show -p stash@{N}` then pop, or record the patch somewhere
  durable first — `archive-before-delete.md` governs a drop.
- **A plant clone: sweep, then delete.** Run
  `core/scripts/daemon-orphan-sweep.sh` FIRST (rb-7489) and confirm the ledger
  is archived. The script never deletes a clone.

**(c) What it deliberately does NOT re-implement.** `git worktree remove` +
`prune` already run inside `promote-to-upstream.sh` (its `_wt_teardown` site
calls `worktree-teardown.sh`); postflight only verifies none survived. The
`--auto-merge` status-check parse is owned by g-115-5645 and is untouched here.

## Cross-references

- `core/config/conventions/pull-promotion.md` — the PULL side: how a
  downstream Mind ADOPTS a tagged release in its own idle window. This file
  is dev→staging (push); that one is staging→downstream (pull).

- `.claude/rules/promotion-cycle.md` — the chain, sign-off rules, drift gate
- `seed-plant-living-prod-safety` (tree) — blast-radius classes per plant step
- `seed-publication-methodology` (tree) — source-stays-dirty transform model
- `core/scripts/promotion-plan-triage.py` — mechanical triage of plan verdicts
- `core/scripts/promotion-preflight.py` — reconcile-UP work-list generator
- `core/scripts/promotion-git-state.py` — Phase 0 freshness + Phase 7
  postflight (git STATE; the preflight sibling audits content DRIFT)
- g-115-4389 — auto-excusal of DEST-FROZEN/SEED-MOTION inside the plan itself
- g-115-4391 / g-115-4392 — the two known dest-suite red classes, tracked
