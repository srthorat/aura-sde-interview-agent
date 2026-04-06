# Engineering Interview Grading Rubric

Adapted from Medium's internal engineering interview rubric.

## Grades
Five-point scale: **Strong No → No → Mixed → Yes → Strong Yes**
- **Mixed** is considered more No than Yes — use it sparingly.
- You do not need to grade every category. Only grade where you have clear, observable evidence.
- For every grade you give, record observable facts — not feelings or assumptions.

---

## Ability to Build Software

### problem_solving
**Strong No**: Cannot conceive of any solution.
**No**: Stuck at the most naïve approach even with significant hints. Solution misses edge cases.
**Mixed**: Reaches a naïve solution independently but needs help with a better approach. Dives in without thinking first.
**Yes**: Outlines a non-trivial approach independently with at most one or two hints. Asks clarifying questions and handles edge cases when pointed out.
**Strong Yes**: Opens by clarifying scope. Articulates multiple approaches with trade-offs. Recognises edge cases unprompted and solves them. No real interviewer help needed.

### code_fluency
*(Assessed on verbal pseudocode, live coding if applicable, or code review discussions.)*
**Strong No**: Does not know basic constructs like loops, conditionals, or function calls.
**No**: Cannot translate thoughts into code. Nonsensical variable names. No standard-library awareness.
**Mixed**: Basic code works but ignores language idioms. Re-implements standard library utilities.
**Yes**: Codes fluently and naturally. Uses standard library. Uses placeholders to abstract complexity and fills them in.
**Strong Yes**: No significant pauses. Idiomatic code by default. Reads provided code quickly and spots non-idiomatic patterns.

### autonomy
**Strong No**: Requires hand-holding through every step. Refuses to decide without consent.
**No**: Needs lots of support. Seeks approval and validation for each decision.
**Mixed**: Works independently but seeks regular approval in a way that abdicates responsibility.
**Yes**: Confident owning decisions. Uses the interviewer as a resource rather than an authority.
**Strong Yes**: Controls the cadence of the interview. Describes rationale without seeking approval. Asks for clarification only where genuinely appropriate.

### cs_fundamentals
**Strong No**: Unfamiliar with common data structures (hash, set, array). Cannot assess relative merits of algorithms.
**No**: Has heard of common data structures but cannot pick the right one. Cannot articulate O(n) vs O(n²) differences in behaviour.
**Mixed**: Uses hashes and sets appropriately but cannot differentiate list / queue / array. Knows some data structures are better-suited for tasks but struggles to say why.
**Yes**: Understands time and space complexity (even without formal terms). Describes trade-offs. Correctly chooses and implements appropriate data structures. Can implement recursion where appropriate.
**Strong Yes**: Deep knowledge beyond basics — heaps, priority queues, tries, bloom filters. Strong intuition for relative merits and when to use each.

### system_design
**Strong No**: Writes everything in one function. Does not see the value in decomposition. Fails to define any reasonable abstractions.
**No**: Does not break code into reusable components. Cannot describe system component interactions. No separation of concerns.
**Mixed**: Reactive, one-step-at-a-time approach. Output looks bolted together. Abstractions are leaky or rigid.
**Yes**: Separates concerns appropriately with clean interfaces. Demonstrates SOLID understanding (even without terms). Functions minimise complexity.
**Strong Yes**: Breaks complex systems into elegant components. Describes interaction model and interface behaviour clearly. Considers race conditions, idempotency, and future extensibility.

### resoluteness
**Strong No**: Ambivalent about failing to finish. Cannot describe persisting through a hard problem. Quit because something was too difficult.
**No**: Shows no strong desire to finish. Sees obstacles as barriers, not challenges.
**Mixed**: Professes desire to finish but shows frustration when issues arise. In past roles, took no steps to improve a difficult situation.
**Yes**: Motivated to fix issues when confronted with them. Describes perseverance leading to a good outcome. Frames prior challenges as growth.
**Strong Yes**: Very determined to finish. If time runs out, expresses genuine disappointment. Has demonstrated extraordinary staying power in prior roles.

---

## Ability to Learn and Teach

### communication
**Strong No**: Asks no questions. Fails to solve the right problem as a result. Cannot describe a concept they know well.
**No**: Intent frequently unclear. Cuts off the interviewer. One-word answers. Cannot explain their approach.
**Mixed**: Has to restate things multiple times. High-level ideas land but details are vague or hand-waved. Cannot describe a time they influenced someone through communication.
**Yes**: Asks clarifying questions. Describes approach at a high level unprompted. Explains complex topics accessibly. Matches vocabulary to the audience.
**Strong Yes**: Checks understanding when explaining complex ideas. Finds the precise word they need. Can explain exactly why they took each step. Persuades sceptical audiences.

### curiosity
**Strong No**: No demonstrated interest in the world around them. No evidence of wanting to learn.
**No**: Cannot describe any self-directed learning. Accepts statements at face value without digging deeper. Asks no questions.
**Mixed**: Has asked "why?" once or twice but is not particularly interested in the answers. Standard questions only (tech stack, working hours).
**Yes**: Asks insightful follow-up questions. Describes independent research on any topic. Derives satisfaction from understanding why things work the way they do.
**Strong Yes**: Insatiable learning appetite. Gets excited at learning opportunities. Runs out of time asking questions.

### awareness
**Strong No**: Significant lack of self-awareness. Cannot identify any areas for improvement. Uncritically gives themselves full marks everywhere.
**No**: Gives false-modest answers ("I work too hard"). Cannot reflect on their performance relative to peers. Has rejected concrete feedback.
**Mixed**: Recognises room to improve but speaks in vague terms. Ambivalent about receiving feedback.
**Yes**: Describes receiving critical feedback and integrating it. Makes reflective statements about growth. Has made deliberate career choices based on self-knowledge.
**Strong Yes**: Critiques own past performance with specificity. Identifies how they have improved and what steps they took. Eager to receive feedback — demands it.

### collaboration
**Strong No**: Describes themselves as a loner. Cannot articulate the value of working in teams.
**No**: Credits "I" for success, "we" for failure. Does not use the interviewer as a resource when stuck.
**Mixed**: Works with others but with some reluctance. Asks some questions but conversation stays shallow.
**Yes**: Happy working with others. Solicits feedback and integrates it. Brings others along rather than just assigning or receiving tasks.
**Strong Yes**: Multiple strong examples of achieving outcomes with others. Eager to collaborate. Explicitly attributes others' contributions and values skills they themselves lack. Uses "we" for shared achievements.

---

## Values Alignment

### do_hard_things
**Strong No**: Has chosen easy positions because they were easy. Shies away from difficult work.
**No**: Stayed in a non-challenging role for a long time with no desire to push boundaries. Reluctant to attempt something they might fail at.
**Mixed**: Has worked on some hard projects but only at others' insistence. Does not display appetite for it.
**Yes**: Has willingly taken on challenges with genuine risk. Has succeeded without a clear roadmap. Takes on hard, unglamorous work because it is necessary.
**Strong Yes**: Has worked on incredibly difficult projects and invented novel solutions. Recognised by peers as innovative. Has executed turnarounds. Has succeeded despite structural barriers.

### level_up
**Strong No**: Satisfied with current capabilities. Considers their learning done.
**No**: Does not actively improve. Career has plateaued. Does not learn from mistakes.
**Mixed**: Half-hearted attempts at self-improvement. Career is stop-start.
**Yes**: Deliberately works outside their comfort zone. Continuously learning (courses, reading, practice). Steady career progression.
**Strong Yes**: Never satisfied with their competency. Can name multiple specific areas they are actively improving. Exceptionally steep career trajectory driven by deliberate self-improvement.

### time_is_precious
**Strong No**: No sense of urgency. Content to do average work. No demonstrated history of hitting deadlines.
**No**: Works hard but is unproductive. Does not time-box. Sees no value in deadlines.
**Mixed**: Recognises deadlines but is content with conservative timelines. Sometimes late with no prior warning.
**Yes**: Healthy sense of urgency. Prioritises competing needs effectively. Balances ideology with pragmatism. Gives timely updates. Front-loads work to leave buffer.
**Strong Yes**: Strong bias to shipping fast and improving. Very respectful of others' time. Sets aggressive but reasonable deadlines and meets them. Constantly optimises for efficiency.

---

## Overall Grade Guidelines

**Strong No**: Hiring would cause significant harm. A single Strong No likely results in rejection.
**No**: Candidate is not right for the role. Strong No in any personality/values category → at least overall No. Uncorrectable technical deficiencies.
**Yes**: Majority Yes/Strong Yes in personality and values — acceptable even with one or two technical No grades. We can teach missing technical skills.
**Strong Yes**: Reserve for candidates who would be exceptional hires. Use with discipline.
