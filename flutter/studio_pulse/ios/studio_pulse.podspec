Pod::Spec.new do |s|
  s.name             = 'studio_pulse'
  s.version          = '2.0.0'
  s.summary          = 'Studio Pulse analytics SDK: install source and device context for the Dart core.'
  s.description      = <<-DESC
iOS side of the studio_pulse Flutter plugin: AppTransaction / receipt install source,
app version and build, hardware model, RAM and the Application Support directory.
                       DESC
  s.homepage         = 'https://github.com/emirhann-akca/studio-pulse'
  s.license          = { :file => '../LICENSE' }
  s.author           = { 'Studio Pulse' => 'https://github.com/emirhann-akca/studio-pulse' }
  s.source           = { :path => '.' }
  s.source_files     = 'Classes/**/*'
  s.dependency 'Flutter'
  s.platform = :ios, '13.0'
  s.pod_target_xcconfig = { 'DEFINES_MODULE' => 'YES', 'EXCLUDED_ARCHS[sdk=iphonesimulator*]' => 'i386' }
  s.swift_version = '5.0'
  # Collected data types and the queue's file-size reads (C617.1), generated from
  # contract/pulse_v2.yaml (python -m studio_pulse.contract.generate).
  s.resource_bundles = {'studio_pulse_privacy' => ['Resources/PrivacyInfo.xcprivacy']}
end
