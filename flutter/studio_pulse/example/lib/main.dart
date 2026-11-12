// Studio Pulse in a Flutter app: init once, then one line per thing that matters.
import 'package:flutter/material.dart';
import 'package:studio_pulse/studio_pulse.dart';

final perfStart = PulsePerf.markProcessStart();

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await StudioPulse.init(
    writeKey: 'sp_pub_local',
    endpoint: 'http://10.0.2.2:8787', // the laptop running `pulse collect`, from an Android emulator
    processStart: perfStart,
  );
  runApp(const ExampleApp());
}

class ExampleApp extends StatelessWidget {
  const ExampleApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      home: Scaffold(
        appBar: AppBar(title: const Text('Studio Pulse')),
        body: Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: <Widget>[
              ElevatedButton(onPressed: () => StudioPulse.levelStart(1), child: const Text('Start level 1')),
              ElevatedButton(
                onPressed: () => StudioPulse.levelFail(1, 'out_of_time', x: 0.4, y: 0.7, progress: 0.6),
                child: const Text('Fail level 1'),
              ),
              ElevatedButton(
                onPressed: () => StudioPulse.settingToggle(PulseSetting.music, from: true, to: false),
                child: const Text('Music off'),
              ),
              Text('install id: ${StudioPulse.installId ?? '-'}'),
            ],
          ),
        ),
      ),
    );
  }
}
