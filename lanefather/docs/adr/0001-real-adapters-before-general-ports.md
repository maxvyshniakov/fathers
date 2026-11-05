# Keep real adapters at the command seam

Lanefather keeps real Git repositories and its existing controlled command tests rather than adding generic Git
and clock interfaces across the engine. An interface that callers must learn is justified when actual production
and test adapters cross it; hypothetical adapters would duplicate orchestration semantics and move the bugs into
untested caller sequencing. Reconsider a port when a second adapter is needed or a concrete failure cannot be
controlled through the existing seam. The reviewer and slotfather process query are real seams and are explicit
modules in this pass.
