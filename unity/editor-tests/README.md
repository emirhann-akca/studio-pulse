# Unity Editor tests of the package

A minimal Unity project that uses `../com.studio.pulse` as a local package and runs the Editor-side
tests (`StudioAppRelease`, `StudioAppSetup`, `PulseAttemptTrace`, `StudioApp.Launch`). They live
here, not in the package, so a game that embeds the package never compiles or runs them.

```bash
Unity -batchmode -nographics -projectPath unity/editor-tests -buildTarget iOS \
  -runTests -testPlatform EditMode -testResults /tmp/results.xml -logFile /tmp/tests.log
```

Needs a Unity licence (CI has none: the pure rules are tested there in `unity/dotnet`). Last run:
Unity 6000.3.23f1, 20 Nov 2026, all passed.
