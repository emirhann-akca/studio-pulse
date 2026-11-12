/// The pure-Dart core of the Studio Pulse SDK (no Flutter imports): usable from a Dart
/// CLI or server, and what the Flutter facade (`package:studio_pulse/studio_pulse.dart`)
/// runs on. Event contract v2: docs/CONTRACT.md in the repository.
library;

export 'src/contract.g.dart';
export 'src/core/core.dart';
export 'src/core/environment.dart';
export 'src/core/json_writer.dart';
export 'src/core/options.dart';
export 'src/core/text.dart';
