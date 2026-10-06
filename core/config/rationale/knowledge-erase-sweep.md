# Rationale: The 30-day erase of a forgotten item

Referenced from `core/scripts/knowledge_erase.py` and `core/scripts/inbound_drain.py` (THE 30-DAY ERASE).
Why the erase of what a member forgot looks the way it does, and, as plainly, what it leaves open.

## Why the receipt holds no text, and what stands in for the archive

`archive-before-delete.md` asks for an independent, verified archive of what is about to be destroyed,
and it says in so many words that an authorization to delete does not waive that step: the
authorization sets the goal and the protocol sets the method. Here the goal is the owner's ruling on
g-335-1726 (a soft forget with a 30-day undo, then erase), and the data to be destroyed is the retained
copy of text a member asked the resident to forget. An archive of that text is the thing the ruling
removes, so the protocol cannot be followed in its letter and the erase kept at the same time. The
method chosen keeps every other half of the protocol and replaces the one copy that cannot exist:

- **Enumerate.** The erase is enumerated by its kind, its window (`forgotten_at`, `undo_until`) and the
  length of the retained bytes. Nothing else about the record is written down: not its item id, and not
  its file name (see below).
- **Verify the layer that is being relied on.** The 30-day retained copy WAS the recovery layer, in
  time. It is read back through `read_record`, which checks the digest of the retained bytes, before
  anything depends on it, and it is re-read just before the delete and skipped if its window or digest
  moved (guard-3881: the decision was made on a snapshot).
- **Delete through the backend.** `LocalBackend.delete`, read back: it raises if the file is still there
  and returns `False` if it was already gone. LocalBackend and not the process-wide backend, for the
  reason the drain lock gives: the spool is a directory on this host or its shared mount, never a
  governed path, and an own-cloud box would otherwise ask the wrong store.
- **Receipt.** `<env>/erasures.jsonl`, beside the lanes, outside the retention directory it describes and
  outside the world, append-only, flushed to disk, one line before the delete (`erasing`) and one after
  (`erased`, or `gone` when another pass deleted the file first). Both lines carry the erase's random id.
  A stop between them is visible as an `erasing` whose id has no closing line. Each closing line says,
  in its `restore` field, that nothing can be restored and why.

A receipt carries no text, no item id, no digest of the text and no file name. A hypothesis id carries a
slug of its claim, a digest of short text confirms a guess, and the retained file's name is a digest of
the item's kind and id, so each would be a way back to what was erased. That last one was measured, not
guessed: the first version named each erase by the retained file, and a probe holding three guessed
hypothesis ids confirmed the right one from the receipt alone (2026-10-03, the review of u4). An erase
is therefore named by a random id (`secrets.token_hex(8)`), and so is the account a pass returns: a
record still on disk is named by its file (an operator has to find it, and a listing of the directory
shows the same name), an erase by its receipt's id, and a pipeline record that has no file by `-`. The
retained record's own `sha256` is checked and then dropped with the record.

This is a decision, made under the agent's decision authority and logged for the owner, and the owner can
overturn it. What would change it: a ruling that the erased text must be archived somewhere (which contradicts
the ruling it serves), or a ruling that a keyed digest in the receipt is wanted.

## Why the world's side comes first and the retained record last

The retained record is what an undo reads, and after the window it is the last thing that holds the text
outside the world. Deleting it first and then failing to blank the page would leave the text in the world with
nothing to say it was ever meant to go. So each record is settled in this order: the world's copy is made
blank and read back, and only then is the retained record erased. A step that fails keeps the record and is
tried again on the next pass; the pass counts it as `failed`, which the drain turns into exit 2 and a runner
finding, so an overdue promise to a member cannot ride out as a clean run.

A failure is one record's. `_attempt` catches it, counts it, and names it by its exception's type and the
`file:line` it was raised from, never its message (a message can quote the text or a path that names an
item), and the pass goes on to the next record, so one record that cannot be settled hides neither the others
nor the counts of what was already done. The drain wraps the whole sweep the same way: an exception is
reported by its type alone, an exit by its integer status (a string exit is a message, and a message can quote
the text), and only an `EraseError` is quoted, since its message names an attribute or says the world cannot be
found, and nothing else.

Everything the pass depends on is resolved before anything is touched: the applier's and the export's names
are checked, the world is resolved (an unset one is an `EraseError`: the export exits with a message of its
own, and the sweep converts that exit instead of passing the message on), and the tree is located. A drift found halfway
leaves a pass half done. Measured on the first version, where the export's names were checked at first use:
with one name missing, the node's retained record was already deleted when the lookup raised.

What the node settle decides, from the page, the index and the retained copy:

- **No tree index at all** is `tree_unavailable` and a failure: nothing can be said about a page without the
  tree it is in. **An index that cannot be read** is `index_unreadable`, since a shown node cannot then be
  told from a forgotten one.
- **No page**, or the tombstone: nothing holds the text. If an index entry for the forgotten key came back over
  the tombstone (a ghost, as a merge from a stale index brings one), it is dropped again.
- **A page that any index entry resolves to** is SHOWN, whichever key holds it. Its forget never took, or
  another key shares the file, and blanking it would destroy a live node, so it is left alone and the retained
  record is erased as inert. The check is over every key, not only the forgotten one: a node re-registered under
  another key was blanked in the first version, which a probe reproduced.
- **A page nothing shows that equals the retained copy** is blanked and read back. This includes the case where
  the forgotten key was re-created at another path: the old page is an orphan that still holds the text.
- **Anything else** is `page_differs`: neither blanked nor erased over, counted `pending`. It may be the
  member's words rewritten (a copy with other line endings was the measured case) or somebody else's text,
  and only a person can say which. The retained copy is what that person would compare it with.

A hypothesis that is still shown is never blanked either, and "shown" is read on the copy the pipeline writer
reaches: the live record, or the archive record when there is no live one. A retained hypothesis record is not
let go on a pipeline that is not there (`pipeline_missing`): a reader returns nothing for a missing file, which
would read as "this record holds no text" and erase the retained copy of a record that is merely not mounted.

A forgotten hypothesis that is still inside its window is not touched at all, not even to blank its supporting
text. An undo restores the statement (`claim`, `title`) and nothing else, so blanking the rest early would hand
the member back a record that lost it. A marker stamped so far ahead that its window cannot be added up (year
9999) is a window that is not over, and a stamp that does not read is over, since nothing could name it in an undo.

## Why prose is decided by shape, and what the shape rule leaves

The forget blanks the statement the member was shown. The rest of a pipeline record is the resident's own
reasoning about it: `rationale` and `position` are keys on all 175 live records (non-empty on 161 and 173),
`outcome_detail` is on 66 of the 69 resolved ones and on no unresolved one, `premortem` on 53 and `interim_evidence`
on 11 (read 2026-10-03 across the discovered, active and resolved stages, 6 + 100 + 69 records). Whether a given
field repeats the claim is not decidable by its name. Those 175 records carry 111 distinct field names, so a list of
the fields that hold prose is an enumeration claim that is wrong the day a writer adds a field (guard-3970). The
sweep therefore blanks by the shape of the VALUE: a string of only ASCII identifier characters (letters, digits,
`_ - . : / +`), of any length, is an id, an enum, a date or a ref and stays; an ISO date, or a date with a time, is
lifecycle and stays; the exact forgotten marker (its head, a date and a full stop) is already blank; anything else
is text and is replaced with the marker. Whitespace is not the test, because a Chinese claim has none. A sentence
that merely STARTS like the marker is text. A container that holds any text, in a value or in a mapping KEY (nothing
stops a writer keying a mapping by a sentence, and the first version missed it), is emptied to its own type (`{}` or
`[]`), never replaced with a string, because other readers of the pipeline iterate every record and a changed type
is a regression for them.

Two short name rules sit beside it, and "never by name" is therefore not the claim. A list of names is never blanked
whatever it holds (`id`, `slug`, `stage`, `horizon`, `type`, `category`, `outcome` and the two stamps), because the
pipeline and the member's own scoping depend on them. The statement fields are blanked whatever their shape, since a
one-word claim reads as an id.

What the rule leaves: a hyphenated sentence of identifier characters and a bare URL pass as ids at any length (a
probe showed both stay), a one-word value in a free-text field (`lesson: "Never"`) stays, and so does a category
name, which can narrow what was forgotten. The id and the slug carry a few words of the claim, and they are the
record's identity. A record nested deeper than the interpreter's recursion limit fails in `blank_plan` while
`json.loads` still reads it (measured: ok at 450 levels, a failure at 600, with the file reading at 900); that is
that record's failure on every pass, counted and named, and not a crash. The tests pin each of these, so a change
is a decision.

## How a guardrail is erased, and where it is not

A guardrail's rule is immutable by design (guard-6210): the store's merge keys a guardrail on its creation stamp and
the whole rule, so a rule edited in place forks the record at the next cross-box merge (rb-5511 measured 11 forked
pairs in the live store). A forget therefore retires the record and leaves the rule in it. Until g-335-1726 u3b the
retained record of a forgotten guardrail was kept and every pass reported it `pending`, because no primitive removed
the rule.

The primitive is one flag and two preconditions. `POST /v1/store/set-field` takes `erase=1`, which lifts the
immutability of the fields a store names in `erasable_fields`. The guardrails store names `rule` and nothing else
(`created` is the other half of its identity), and `rule` is exactly what a forget removes
(`item_text_fields("guardrail")`; `test_the_erase_mode_unlocks_only_the_rule_and_only_what_a_forget_removes` pins
the two as one table). The write is refused unless (a) the record is
`retired`, read again inside the lock (`409 erase_not_retired`), and (b) the daemon's backend is `LocalBackend`
(`409 erase_not_local`). Anything that stops the backend from being read as local is a refusal. The sweep maps the
second refusal to `pending` and keeps the retained record. The rule is a record's FIRST write for that reason, so on a
backend that refuses it nothing of a record whose rule is still to blank has changed (a pass that finds every rule
already blank, which only a backend that changed between passes can leave, may still blank other fields first: those
are ordinary writes and any backend takes them). A dry run cannot ask the daemon, so what it reports for a guardrail
with a rule to blank carries `backend_unchecked`.

Why a precondition and not a tombstone. The earlier plan was a primitive that removes the text and leaves a merge
tombstone, so that a stale copy cannot bring the rule back. That needs the guardrail merge to understand a record whose
identity changed, and it is only needed if a second copy of the store can exist. The premise check of u7 (2026-10-03,
read on the sidecar root: 171 mind workspaces, 166 with a `.env.local`, all 166 `STORAGE_BACKEND=local`, one `.git`
directory with no remote) found a member's home to be one directory on shared storage with no merge channel: own-cloud
sync is armed only by `STORAGE_BACKEND=own-cloud`, and a git merge needs a remote. So the erase is made safe by refusing
it wherever a merge could occur, and no stale copy exists to resurrect the rule. The re-open trigger is the vessel
rationale's own: a home gaining a claim store or a remote. The git-remote channel is NOT checked by the daemon, so a
local-backend home that gained a remote would be erased in place; it is named here and not guarded, and a tripwire for a
home whose backend drifts was relayed for filing at the end of u7.

What the sweep blanks follows the hypothesis rule (above), by shape: the rule whatever it holds, every other field whose value
is text, and a container that holds text emptied to its own type. Identity and lifecycle stay (`id`, `created`,
`category`, `status`, `severity`, the retirement stamps, tags made of identifiers). The reason a correction retires a
guardrail with is the framework's own sentence, an id and fixed words ("superseded by guard-N: a member corrected this
rule"), so it stays: the lineage walk below reads it.

Measured on a copy of this box's live guardrail store (7,177 records, 21.2 MB, read 2026-10-03; a member home's store
may differ), with each record forced to the forgotten state: the plan blanks `rule` and `trigger_condition` on every
record, `action_hint` on 3,149, `title` on 1,466, `source` on 713, `when_to_use` on 593, `trigger_pattern` on 34,
`context_triggers` on 12, `amended_fields` on 6, `tags` on 2 and `phases` on 1. The six `amended_fields` are real: those
records store a free-text note in a stamp map's value, so the whole map is emptied, which costs the merge-ordering
stamps of a record that is retired and erased. Applied the way the daemon applies it (one field at a time, the
defaults healed, the amendment stamp added), the result is accepted by the store's own validator for all 7,177 records
and a second plan over it is empty for all 7,177, so no live record would make a pass report `text_remains` for ever.

The lineage. A member's Undo does not reactivate a retired guardrail (the merge keeps a retirement over a stale active
copy), so the rule comes back as a NEW record tagged `restores:<id>`. A correction adds a successor tagged
`supersedes:<id>` and retires the old record with the sentence above. So the text of a forgotten rule is also held by the
records an Undo or a correction left behind, and none of them has a retained record of its own: forgetting only the last
one would leave the first one's text for good. The sweep walks them from the forgotten record. A tag names a predecessor,
and the link is believed only when the predecessor agrees: a restore needs it to have been retired by a member's forget,
a correction needs the reason it was retired with to name the successor. A tag is a string anybody may write, and an
active guardrail is never a predecessor. The walk is bounded at 32 links (`lineage_too_deep` is a failure) and runs
predecessors first, the forgotten record last: a record is blanked only after the records its tags lead to, because its
tags are what a later pass would walk, and a record's tags are its last write. A record that fails stops the walk, so no
later record is blanked past it and the retained record stays for the next pass.

The same holds for a retained record that no longer reads (its digest does not match) when it names a node or a
guardrail: its world side, a page to blank or a rule to blank, cannot be checked without it, so it is kept and
counted `pending`. The first version purged it as residue, which a probe showed deletes the evidence of a page that
still holds the text. An unreadable record that names a hypothesis, or names nothing, is purged after 30 days, since
a marked hypothesis is found from the pipeline's own marker, and a write that never finished (`.json.tmp`) is purged
after an hour. Only a file named the way the retention writer names its own is purged; one placed there by hand is
left alone, and a record of a newer schema is kept, since deleting what this code cannot evaluate would be the
opposite of a careful erase.

## Why an undo is judged by when it was sent

The window a member sees is "undo until X", and X is 30 days after the home APPLIED the forget. A stopped home applies
nothing, so an undo sent on day 29 can wait in `inbound/` past day 30. Judged when it is applied, it is refused
`undo_expired` and the same pass erases the retained copy: the member did what the page allowed and lost the item
(the g-335-1726 u5b1 review, finding F-2).

The applier therefore judges an undo at the earlier of the time its queued record carries (`queued_at`, handed to the
applier as `--queued-at`) and now (`knowledge_retention.judged_at`, the one place that rule lives). The clamp keeps the
change one-directional: a stamp can make the judgment more lenient than applying now and never stricter, so no undo that
is accepted today is refused by it. A stamp that is absent, not text, or does not parse is judged at now, which is the
behaviour before this change, and a stamp with no offset is read as UTC whatever zone the box is in (a test sets the
zone to settle it).

The sweep runs after the queued records, so an undo sent in time has restored the item and left a marker before the sweep
looks, and the sweep passes over a marker. A pass that leaves queued records behind does not sweep: records past an
operator's `--max` cap (the fleet's wrapper passes none), or records left unclaimed because the box cannot resolve
handles. Either could hold an undo whose retained copy the sweep would erase first, so the pass reports one `deferred`
entry in `erasures` (a count; no text, no id), changes no counter and does not fail, and the next pass that leaves
nothing behind sweeps. Erasing a pass late keeps a retained copy one pass longer; erasing early cannot be taken back.

## Why it runs inside the drain

The drain already owns the three things an erase needs: the environment's lock (a sweep that runs beside a member's
undo could delete the record the undo is reading, or blank text the undo has just put back), the two fences a forget
takes (a box that is not the environment must not touch its retention or its world), and the exit code and finding
channel that reach an operator. The sweep runs after the queued records so an undo among them is applied, or refused
as expired, before the record it would have used is deleted (the next section says how that judgment is made), and
it is skipped where no retention directory exists, so an environment that never handled a forget is untouched and an
idle box with a wrong fence reports nothing it never had.

The lock is only as good as its staleness bound. `LOCK_STALE_SECONDS` is 600, and the sweep's longest steps are
daemon writes: one blanked hypothesis took five `update-field` calls in a probe, and the transport's default
timeout is 30 seconds per call (`_rt.py`), so a slow daemon spends 150 of the 600 seconds on one record, and
four records exhaust the bound. A second drain would then break a lock whose holder is alive and run beside it.
The sweep therefore calls a `tick` before each file, each settled record and each write, and an applying drain
passes one that refreshes the lock's mtime. A dry run holds no lock and passes none.

## What it leaves open

Each of these is counted or pinned, never reported as erased:

- **The stamp is the intake's, and a member's request cannot set it.** An undo is judged by `queued_at` as the queued
  record carries it. Measured 2026-10-04 from the intake's source, read on its development and its production branch
  (they differ only in the `undo` op): the intake stamps `queued_at` itself, from its own UTC clock to the second
  (`%Y-%m-%dT%H:%M:%S`, no offset, the form the fixtures use and the box reads as UTC). It builds the record's `kind`,
  `environmentKey`, `accountId`, `queued_at` and `source` first and only then merges the fields its validators return,
  and those return `handle`, `op`, `text` and `base` for a knowledge change (`handle`, `verb` and `value` for a member
  verb, `text` for a directive), so no request body can supply or overwrite the stamp. What remains is a writer that
  reaches the spool without the intake: an operator, whose requeue (`requeue_stale`) moves the file and keeps its
  original stamp. A stamp from such a writer that lied about being early would let an undo through until the first
  applying pass erases the retained copy, which on a stopped home is as long as the home is stopped. NOT measured: any
  writer beyond the intake and an operator, and the deployed function's own artifact hash (its deploy job asserts it,
  and the last runs on both branches passed). Re-derive: read the intake's source on each branch, and check that
  `queued_at` is assigned before the fields are merged and that no validator returns it.
- **A failed undo holds nothing.** A record whose apply FAILED stays in `processing/` and is not retried (an operator
  requeues it), so the sweep does not wait for it: its retained copy is erased once the window is over, and a requeue
  after that is refused. Waiting would let one stuck record keep every erase in the environment from ever running.
- **The world's history.** Every pipeline write snapshots the file first, so the pre-forget text survives in the
  history store, and the prune policy keeps one weekly snapshot for day 31 onward with no stated end
  (`history.md`, Prune old snapshots). The erase's own writes add snapshots of their own (a probe counted four files
  holding one supporting-text string before a pass and five after). A test pins it
  (`test_the_pipelines_own_history_still_holds_the_text_after_the_erase`). Node bodies are not versioned there
  (`history.md`, measured 2026-08-10), but the tree index is, and an index entry carries a `summary` derived from
  the page; that residue is stated from the convention and was not measured here, because a tmp world does not
  reach the history classifier. Removing it means vacuuming version history, a destructive act on a recovery layer
  that is the owner's to rule on.
- **The append-only archive file.** A copy of a hypothesis in `pipeline-archive.jsonl` beside a live copy cannot be
  written through the pipeline writer, which reaches the live one. It is counted as `pending` residue every pass
  and the retained record is still erased, since keeping it would protect nothing. An archive-only record is
  blanked through the writer.
- **The guardrails store's history.** Every store write snapshots the file first, so the pre-forget rule survives in
  the history store, and the erase's own writes add snapshots of their own. Same policy and same reasoning as the
  pipeline's, and a test pins it (`test_the_guardrails_own_history_still_holds_the_text_after_the_erase`).
- **A guardrail the store's validator refuses.** Every write validates the whole record, so one that carries a field the
  store does not know cannot be written at all. The sweep refuses a name the writer would land on a neighbour
  (`unwritable_name`) and counts any record whose write fails as `failed` on every pass, keeping the retained
  record: loud, and never erased over text. It needs an operator. None of the 7,177 records in the live copy above is
  refused, before or after its plan.
- **The cost of a guardrail write.** A store write rewrites the whole file and snapshots it first. Measured in process on
  the 21.2 MB copy, seven `set-field` calls took 0.94 to 1.10 s each and left 14 files and 48.6 MB under `.history`, about
  7 MB a write. A guardrail with four fields to blank is therefore about four seconds and 28 MB of history, and a lineage
  multiplies it. The sweep shows the drain lock is held before each write, so the 600-second staleness bound is not the
  limit; the history growth is, and whether a member home's store is that size is unmeasured.
- **Immutability is a property of one route.** `set-field` is the only store route that enforces `immutable_fields`;
  `replace` and `merge` do not (read in the review of this unit, 2026-10-03). The only framework callers of those two are
  `pattern-signatures-update.sh`, `journal-update.sh` and `journal-merge.sh`, none of them for guardrails, so the erase
  mode makes `rule` no less immutable than it was: it names one authorized path through the route that checks.
- **Text a lineage tag does not name.** The walk follows the two tags the applier writes. A guardrail copied by hand, or
  a rule's wording the resident folded into another record or note, is not reached.
- **A home on another backend, or with a git remote.** The first is refused and counted `pending`. The second is not
  checked (above).
- **One-word text.** A value of one identifier word in a free-text field (`title: "Deploy"`) reads as an id by shape and
  stays, as for a hypothesis.
- **The retrieval embedding index.** `embedding-index-build.py` persists, per document, a float16 vector derived from
  the document's text and an `id`, `type` and `hash` row in `meta.json`, where the hash is a digest of that text. Its
  corpus is guardrails, reasoning-bank entries, pattern signatures, tree nodes and framework docs (hypotheses are not
  in it). After an erase, the row for a blanked guardrail or node still carries the old vector and digest until the
  next `--update` re-embeds it, and nothing in the sweep runs one. It is a per-box cache under the daemon's state
  directory. Whether a member home builds one is unverified: there is none on the box this was written on (read
  2026-10-03T13:15, the directory is absent). A digest of short text confirms a guess, which is why the receipts carry none.
- **A field name the writer will not take.** The writer refuses a dotted name, and it strips a name's whitespace,
  so a write to `"category "` would land on `category`, the member's own scoping. The sweep refuses both
  (`_writable`) and counts the copy as residue instead of retrying forever or overwriting a neighbour.
- **The cross-box merge.** `coordination_merge._merge_pipeline_record` carries the statement and the two stamps as one
  group; the supporting text is merged field by field, so a stale copy from another box can bring blanked supporting
  text back. It needs a second box holding a stale copy, so it is a hardening unit for multi-box worlds; that an
  environment is served by one box was not verified for every deployment.
- **The spool volume's snapshots and backups** are not checked. The delete is final on the volume the drain sees.
- **No overwrite before the unlink.** The file systems this runs on make that unreliable.
- **Where the drain runs.** A vessel that is stopped keeps the retained text past 30 days; the sweep runs at the next drain.
- **The copies an export makes.** The member's panel, the saved copy in Vinheim's file storage and the member's
  download come from `knowledge-export.sh`, not from the stores: it writes the home's `.knowledge-bundle.json` and its
  OKF wiki folder, then publishes the bundle to Vinheim's file storage (one key per home, replaced by each save). Each
  carries a page's whole body and a hypothesis's statement. The export runs from an hourly timer and at home stop and
  nothing else, so the apply does not refresh it and the erase neither refreshes nor touches it: an item leaves all
  three at the next successful export after the apply (the projection drops a forgotten hypothesis, a retired rule and
  a page whose index entry is gone). A failed or oversized publish keeps the previous saved copy, and a failed export
  keeps the previous files. Read in `knowledge_projection.py` (`_exposed_knowledge`), `knowledge-export.sh` sections
  1-3 and the sidecar timer and stop hook (Mind-Environment-Server `ops/mind-sidecar`); the daemon that serves the
  file was not read. Found by g-335-1726 u8 (2026-10-03).
- **Earlier saved versions.** The bucket behind that saved copy (`vinheim-data`) is versioned, so a replaced copy is
  kept as an earlier version and a delete adds a delete marker. Measured 2026-10-03 on a fleet smoke-fixture prefix
  only (counts and dates, never a member key): 49 versions, all non-current; 49 delete markers, all current; no
  unversioned object; the oldest delete marker dated 2026-08-22 with its version still present, so no non-current
  expiry under 42 days applies there. `GetBucketVersioning` and `GetBucketLifecycleConfiguration` were refused for both
  principals a box can reach (the fleet user, and the operator role, which did answer a location read), so the
  lifecycle rules are not read and a rule scoped to member prefixes is not excluded. Expiring or purging non-current
  versions destroys a recovery layer, so it is the owner's to rule on (`archive-before-delete.md`).
- **The write-ahead gap.** An `erasing` line whose id has no closing line after it means the delete did not finish (a
  stop, or a delete the backend refused, which the next pass retries and so writes a second `erasing` for, under a
  new id) or that its closing receipt did not land. Read the last line per receipt id, not the count of lines.
- **Two sweeps at once.** The drain lock keeps them apart, but a direct caller of the applier is not under it. A second
  sweep that meets a file the first one already deleted writes `gone` and counts nothing, so one file is one erase.
- **The index is read once per node.** It is read before the page, and a key registered in the milliseconds between
  that read and the blank would have its page blanked. The applier's own forget has the same blind spot. Named, not closed.
- **Two ways of finding the world.** The fences resolve the world root with `MIND_WORLD` first and then the conf,
  while the export resolver reads `WORLD_PATH` first and then `MIND_WORLD`. A box that sets both to different places
  would pass the fence for one and sweep the other. The forget has the same property. Unverified on such a box.
- **A record stored with a padded id.** The scan strips ids, so a record stored as `" id "` is found as `id`; the
  pipeline writer strips the id it is asked for and matches the stored one exactly, so it cannot reach that record.
  The write is refused, the record counts as `failed` with its retained record kept, and it says so on every pass until
  a person corrects the stored id: loud and permanent, never an erase over text that is still there. Pinned for a
  marker-only record and for one with a retained record.
- **The applier's own diagnostics.** A write that fails prints `write failed for hypothesis <item id> <field>` on stderr
  (`knowledge-edit-apply.py`, for every caller), and a hypothesis id carries a slug of its claim, so the drain's stderr
  log can name an item the receipts and entries deliberately do not. Not changed here; relayed as a finding.
- **A record with no file is named `-`.** An operator finds such a pipeline record by its marker (`forgotten_at`) in
  the pipeline, and not by name, since a name would be a digest of its id.

## Cross-references

- `.claude/rules/archive-before-delete.md` — the protocol this adapts; its ENUMERATE, VERIFY LAYERS, DELETE and RECEIPT
  steps are kept and its ARCHIVE step is the one that cannot be.
- `core/config/conventions/pipeline.md` — "A member's forget and undo, and the merge".
- `core/config/conventions/history.md` — what is and is not versioned.
- `core/scripts/knowledge_retention.py` — the retained record, its window, and the reader the sweep relies on.
- `core/scripts/tests/test_knowledge_erase_sweep.py` and `TestTheErasureThroughTheDrain` in
  `core/scripts/tests/test_inbound_drain_engine.py` — every claim above that is a behaviour is pinned there.
