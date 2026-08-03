# ZeAlfie Architecture Notes

ZeAlfie is an orchestrator for the ZeSoftware astronomy ecosystem. It must keep ZeSolver, ZeMosaic, ZeAnalyser, and ZeSeestarStacker independently developed, tested, versioned, and publishable.

For the first milestone, ZeAlfie exposes only a minimal command-line surface. It does not import, detect, configure, install, update, or launch any external component.

The target component boundary is a stable public contract, not direct imports from GUI internals. For ZeSolver, the preferred integration direction is an independent launch through a published entry point or subprocess command, with version and capability information read from package metadata or a small public diagnostic interface.

Future GUI work should reuse the application logic behind the CLI. PySide6 is the intended GUI framework for ZeAlfie, but it is intentionally not a dependency until GUI code exists.
