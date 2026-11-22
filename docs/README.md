# Documentation

Where each final-project deliverable is:

| # | Deliverable | File |
|---|---|---|
| 1 | Prompts | `../PROMPTS.md` (build prompts session by session, the development process and the context given to the agent, the AI agent's own prompts) |
| 2 | Acceptance test cases, executed, with screenshots of every window and pop-up | `ACCEPTANCE_TESTS.md`, images in `acceptance/img/`, run by `../tests/acceptance/run_acceptance.py` |
| 3 | UML class diagram | `ARCHITECTURE.md` section 3 (design view, `img/class_diagram_design.png`); full generated diagram `../CLASS_DIAGRAM.md` (`img/class_diagram_full.png`) |
| 4 | System statechart diagrams | `STATECHARTS.md` (`img/statechart_*.png`) |
| 5 | Structure of the system and its critical algorithms | `ARCHITECTURE.md` |
| 6 | Source code | the repository (archive: `git archive`, see the submission zip) |
| 7 | API docs (javadoc equivalent for Python) | `api/index.html` (generated from the docstrings with pdoc) |
| 8 | Unit tests for the Model and Controller classes | `../tests/unit/`, described in `../tests/README.md` |
| 10 | Presentation | `presentation/HCV_Final_Presentation.pptx` (built by `presentation/build_deck.js`) |

Requirements: `../SRS_HardwareComputeVisualizer.md`, `../ProjectInfo.md`. User guide: `../README.md`.

Regenerate:

    python docs/render_diagrams.py                 # docs/img/*.png from the Mermaid sources
    python gen_diagram.py                          # CLASS_DIAGRAM.md from the code
    python -m pdoc --docformat google -o docs/api main telemetry nvgpu workloads analysis agent sandbox_run remote remote_agent profiler control mcp_server
    python tests/unit/run.py --coverage            # unit tests + coverage
    python tests/acceptance/run_acceptance.py      # acceptance run + screenshots
