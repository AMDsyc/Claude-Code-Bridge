# Quiet mode: running windowed checks so that nobody sees them

A self-contained document. You need to know nothing about this project — the
principles carry over to any engine, any application with a window, and any
pairing of a person with automation. Engine function names appear as examples
in passing: they are not part of the rule.

The full text of rule 29 of the canon. Written by a pair who did this and
checked it with the person sitting in front of the screens.

---

## 1. The problem

Automation launches the application dozens of times a shift. Every windowed
launch creates a window, and that window **takes keyboard focus** away from
the person who is typing at that moment: their letters go into somebody
else's program, and sometimes something in it gets pressed.

The obvious answer — run everything headless (`--headless`) — is incomplete:
a windowless mode compiles no shaders and captures no frames, and frames are
exactly what serves as evidence in a conversation about the picture. So some
of the checks **must** be windowed, and it is those that have to be made
quiet.

The requirement in its strong form: the person must not see a single window
and must not lose input — **on any number of monitors, including ones that
are not there right now**. That last part disqualifies coordinates as the
main mechanism.

---

## 2. Principles

**1. Do not move the window "far away" — arrange for it not to be on any
screen at all.** "Far away" is about today's monitor layout; tomorrow a
screen is added and the solution breaks silently, without a single error in
the log.

**2. A coordinate is not a solution, and that is checked rather than
assumed.** The operating system clamps a window's position back inside the
bounds of the desktop, and it does so **silently**. Measured case: a
1920×1080 window on a 10240×1890 desktop made of three monitors, requested
position (10000, 10000), became (9600, 996) — a 640×444 corner on a live
monitor. Aiming at the edge of the desktop is useless: (10304, 0) produced a
640-pixel strip in the same place.

**3. The main mechanism is a window BORN minimised.** A minimised window is
on no screen by definition, so the number of monitors does not affect it at
all. This is the only known form of invisibility that does not depend on
geometry.

**4. Minimise at birth, not from the application's own code.** Application
code runs only after the window has been created and shown — which leaves a
flash, and that flash is precisely what the person sees. The order "show it
minimised" is given by the operating system as the process starts.
*Windows:* `Start-Process -WindowStyle Minimized`; in the API, `STARTUPINFO`
with `STARTF_USESHOWWINDOW` and `SW_SHOWMINNOACTIVE`. *POSIX:* the window
manager plays that part.

**5. A minimised window stops drawing — that is a defect to be cured, not a
reason to retreat.** The first frame comes out, and after that the
"frame drawn" signal never arrives, so a check waiting for it hangs for good:
measured case, 11 seconds became more than 540 with no result. The cure is to
force a draw every frame; with it the run takes the same 9–11 seconds and
every frame is there.
*Godot 4:* `RenderingServer.force_draw()`.
Giving up minimisation over this means throwing away the one geometrically
independent mechanism because of a fixable detail.

**6. The second lever is to take away the window's right to be focused.**
Even an invisible window must have no right to take input, and that right
should be removed as early after startup as possible. *Godot 4:*
`DisplayServer.window_set_flag(WINDOW_FLAG_NO_FOCUS, true)`.

**7. The default lives in the automation's launch wrapper, NEVER in project
settings.** A project setting will infect the launches a person makes by
hand as well: they press "play" and get a minimised window with no focus. The
place for the default is a wrapper script that only automation goes through.

**8. Quiet is the default and loud is the variable. Not the other way
round.** Inside the application the footprint stays zero: one check right at
the start, an exit, behaviour unchanged. But in the automation's wrapper,
quiet mode cannot be the thing you have to remember to switch on. Measured
case: a build was started with the ordinary command and no quiet-mode
variable — the mode was optional, and that was enough. Inverted: quiet by
default, loud only by an explicit variable, and the gate checks the
**default** (a launch with no variables at all must come out quiet), not only
the path where the variable is set.

**9. Quiet mode must PROVE it is not lying.** The same input seed, one
ordinary run and one quiet run, results compared as numbers. Measured case:
ten checksums at five levels matched to the character; a frame compared
pixel by pixel against the ordinary one gave 16.73% differing pixels against
the run's own noise of 16.00% — that is, the difference is indistinguishable
from noise. Until a discrepancy is explained, quiet mode is not switched on
for runs that are meant to be evidence.

**10. A check must be able to fail.** Matching checksums are worth nothing if
they match always. A third run on a DIFFERENT seed: every checksum must move.
If they do not, you are measuring something other than what you think.

**11. Simulated input must come from inside the application, not through
operating-system events.** Input delivered by the application's own mechanism
needs no focus and survives invisibility. This too is not assumed but
checked, with one control run. *Godot 4:* `Input.action_press`.

**12. Windowless remains the first choice for everything that needs neither
frames nor shaders.** The best way not to show a window is not to create one.
Only the minority of checks that capture pictures or compile shaders is made
windowed.

**13. Name the cost yourself.** Forcing a draw changes the frame pacing.
Everything that depends on the seed matches exactly; everything measured in
frames drifts slightly. Check whether it drifts WITHOUT quiet mode as well:
measured case, two ordinary runs of the same scenario differ in intermediate
values by 0.03 of a unit, which means the instrument is unstable in itself
and quiet mode has nothing to do with it. The verdicts never moved once.

**14. The whole launch path must be invisible, not only the final window.**
This is a class of mistake, not an incident, and it was caught twice in one
day on two different projects and two different shells. In both, the wrapper
started the application through an intermediate process — "run the show
command and then launch this" — and the intermediate process was itself born
with a window. We hid the application and displayed the thing that hides it.
Worse: in the second case the show order never arrived at all. `STARTUPINFO`
goes to the process that is created, and what was being created was the
shell — so the application was born ordinary whatever flag you set.
There is one cure: spawn the application **directly** from the already
running automation process, with no hop through a shell. *Windows:*
`SW_SHOWMINNOACTIVE` in `STARTUPINFO` plus `CREATE_NO_WINDOW`, so that no
console appears either.
It follows that "the launch path" means every entrance: not only the command
you run the check with, but version-control hooks too, and wrappers around
wrappers.

**15. A census, not a guess.** Until the windows are enumerated, the
conversation is about suspects. A window watchdog polls top-level windows
during the run and records every new one: title, class, size, position,
process, and whether it intersects the desktop. **The list of windows is the
list of fixes.** Measured case: the first audit produced five windows, of
which one was ours, 158×26 and outside the desktop; the other four belonged
to somebody else, and without the census they were being taken for our own.

**16. Not every application obeys the show order, and then what is needed is
a different mechanism, not more force.** `wShowWindow` is a request; an
application is entitled to set its own window state, and the measured case is
exactly that: the window is born ordinary, though it does not take focus.
Minimising it from code after startup is not allowed — that is principle 4,
the flash has already happened.
What works instead: **a separate desktop**. Exactly one desktop is drawn, the
desktop name is read by the system at the moment the process is created, and
the application cannot come back to the visible one. The invisibility comes
out of how the system is built rather than out of geometry — so it survives
any number of screens, just as minimisation does. Frames are still captured:
measured case, a screenshot of a window on the hidden desktop, 24 KB,
byte for byte identical to the ordinary run.

**17. A limitation you failed to close is declared a limitation.** The same
case: the flag is ignored by the application, half the effect (focus) is
there and half (minimisation) is not. That is written down as a named limit,
not papered over by minimising after startup, and not passed off as a
success.

---

## 3. Traps

**Minimised ≠ quiet, if you minimise late.** A window minimised by the
application's own code has already managed to show itself. The blink the
person sees IS the window being born; the only cure is a show order from the
operating system.

**Position clamping is silent.** The system does not report that it moved the
window. Always **read the position back** after setting it and compare it
with what you asked for, then check whether the window's rectangle intersects
the desktop's rectangle. If it intersects, that is not quiet, whatever the
number looks like.

**Position at launch and position while running are clamped differently.**
Measured case: the command line was always clamped, while setting the
position of an already running window went through as given. That makes
coordinates fit only for a fallback path, and with the caveat from
principle 2.

**Windowless mode hangs on checks that capture frames.** This is not "no
pictures" — it is waiting for a draw signal that will never come. Split the
checks: those that need only numbers go windowless, those that need frames
take the quiet windowed path.

**A project setting infects manual launches.** The temptation to "just tick
one box in the settings" breaks the person's own work, not only the
automation's.

**Do not minimise a window below the working resolution** if the raster
target matches the window: the frames will come out the wrong size. Check the
size of the captured frame, not only that it exists.

**An intermediate process eats the show order.** A shell started in order to
launch something minimised receives the show order itself — and nothing is
left for the application. From the person's side the symptom is always the
same: a panel blinked. Launch directly (principle 14).

**A census taken from inside the hidden desktop lies, and it lies towards
failure.** A process whose thread is attached to the hidden desktop
enumerates **that** desktop, and its own invisible windows come back as
"visible". Measured case: from inside, two windows on the visible desktop;
from an unattached process, zero. That reads as a failure of exactly the
mechanism that is working, which is why the count is always taken by a
**separate process**, not attached to the hidden desktop.

**Not everything that appeared on the screen is yours, and claiming somebody
else's is as dishonest as hiding your own.** They are separated by an idle
control: a run in which the automation does nothing. Measured case: 75
seconds without a single action produced zero new windows, which means the
four foreign windows that were caught are not born by themselves and were not
born by the run. Whose they are is a question for the owner of the machine;
there is no need to climb into somebody else's lifecycle, but there is a need
to name it.

**A narrow `grep` proves nothing.** "There are no direct launches left" was
said on the strength of a search inside the scripts folder — and turned out
to be wrong. A broad search by the executable's name found a fourth path, and
the most dangerous one, because it is implicit: the launch was wired into a
**dependency** rather than into the project's code, and fired by itself on
every call to that dependency's tools. The "do not show a window" flag was
set there — the very flag this application ignores. Search by the
executable's name across the whole tree, dependencies included.

**A modal dialog is a window too, and it has to be closed quietly.** Sending
a click message to the dialog's button closes it without changing the active
window; going through a keystroke is louder and is fit only as a fallback.
Measured case: on a real system dialog with a real child button the active
window did not change; the keystroke branch on a live application dialog is
**not measured**, and that is said rather than passed over.

---

## 4. Adoption checklist

1. Split the checks: "needs only numbers" — run it windowless; "needs frames
   or shaders" — the windowed path.
2. In the automation's launch wrapper (not in the project settings!) start
   the process with the operating system's "show minimised" order —
   **directly, with no hop through a shell** — and make quiet mode the
   default rather than a flag: the loud path is switched on by an explicit
   variable.
2a. Find ALL the launch paths, not the ones you remember: search by the
   executable's name across the whole tree, dependencies and version-control
   hooks included. Every one you find is wrapped into the same quiet path.
3. At the earliest point in the code: read and print the geometry of every
   screen and your own window's mode **before** interfering; take away
   focusability; hold the minimisation.
4. Switch on forced drawing while the window is minimised, or checks waiting
   for a frame will hang.
5. Prove identity: the same seed ordinarily and quietly — the numbers match,
   the frame is within the run's own noise; on a different seed the numbers
   must move.
6. Check input with one scenario that presses something, and confirm the
   verdict is the same.
6a. Enumerate the windows: the watchdog records every window born during the
   run and says whether it intersects the desktop. Take the count with a
   **separate process**. Run an idle control alongside — a run with no action
   at all — to separate your windows from other people's.
6b. Make the gate two-sided and check that it is able to fail: the quiet path
   must report "window minimised", the loud one "not minimised"; a launch
   with no variables at all must come out quiet. A gate that does not fall
   over under sabotage is not a gate.
7. Give the final acceptance to the person: your machines cannot see their
   screens. Ask them outright whether they saw anything — and do not call the
   mode quiet until they have answered.

---

## Acceptance

Step 7 is not a formality, and it is the only one that cannot be carried out
by a machine. On **2026-08-21** the person was asked outright whether they had
seen anything on any of their three monitors during an hour of runs. The
answer: **"no, I did not"**. From that moment — and not before — the mode was
called quiet.

And they had answered "I did" earlier, when the mode had already been
declared quiet by a machine. Both times, and this is what matters, the
machine's own check passed: the window read "minimised" for itself, honestly
and correctly — what blinked was the wrapper, not the window. **Your own
window is not the whole run.** That is why step 6a stands beside step 7 and
not instead of it.

One more check came free. That same day the person's monitor layout changed —
the desktop became (−4389, 0) sized 5852×1080 instead of the earlier (0, 0)
10240×1890 — and the quiet survived **without a single coordinate being
edited**. That is exactly what the mechanism was made geometrically
independent for (principles 1 and 3); had it rested on coordinates, it would
have broken silently.

---

## What the bridge itself does about this

The bridge opens its session windows already minimised and with no right to
take focus — `sessions.launch` passes `STARTUPINFO` with
`SW_SHOWMINNOACTIVE`, so principle 4 is satisfied in its own house by an
order from the operating system as the window is born, not by code
afterwards. Everything the bridge has no need to show, it runs windowless
(`claude -p` for the archive search and for asking about models) —
principle 12.

Principle 14 is satisfied in its own house too: `sessions.launch` calls
`claude` **directly** — an argument list, no shell in between — so the show
order reaches the process it was meant for rather than an intermediary. The
windowless runs (`claude -p`) go through pipes and open no window of their
own; `CREATE_NO_WINDOW` is not set on that path, and they have no console of
their own only because they inherit the daemon's — that is reasoning, not a
measurement, and it was not measured here.

Rule 29 has no gate and cannot have one: **the bridge cannot see anybody
else's screens.** It can check only its own launch path — and, as that same
day showed, one's own launch path can be flawless while the wrapper around it
blinks. The last word stays with the person, and that is written into the
rule itself.
