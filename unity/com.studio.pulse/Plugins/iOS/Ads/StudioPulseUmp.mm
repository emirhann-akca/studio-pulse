// Google User Messaging Platform for Studio.Pulse.Ads (StudioUmp.cs). Included in an iOS build only
// when the project defines STUDIO_PULSE_UMP and links the UMP SDK (pod GoogleUserMessagingPlatform 3.x).
// UMP reads the AdMob app id from Info.plist (GADApplicationIdentifier; Studio Pulse's build step writes it).
#import <UserMessagingPlatform/UserMessagingPlatform.h>

extern UIViewController *UnityGetGLViewController(void);

typedef void (*StudioPulseUmpCallback)(int canRequestAds, const char *error);
typedef void (*StudioPulseUmpPhase)(void);

static void StudioPulseUmpReply(StudioPulseUmpCallback callback, NSError *error)
{
    if (!callback) return;
    BOOL can = UMPConsentInformation.sharedInstance.canRequestAds;
    dispatch_async(dispatch_get_main_queue(), ^{
        callback(can ? 1 : 0, error ? error.localizedDescription.UTF8String : NULL);
    });
}

extern "C"
{
    // geography: 0 off, 1 EEA, 3 regulated US state, 4 other. testDeviceIds: comma separated or empty.
    // updated() runs once the consent information is fresh (the form may follow); callback() at the very end.
    void StudioPulseUmpGather(int geography, const char *testDeviceIds, StudioPulseUmpPhase updated, StudioPulseUmpCallback callback)
    {
        UMPRequestParameters *parameters = [[UMPRequestParameters alloc] init];
        parameters.tagForUnderAgeOfConsent = NO;
        if (geography > 0)
        {
            UMPDebugSettings *debug = [[UMPDebugSettings alloc] init];
            debug.geography = (UMPDebugGeography)geography;
            NSString *ids = testDeviceIds ? [NSString stringWithUTF8String:testDeviceIds] : @"";
            if (ids.length > 0) debug.testDeviceIdentifiers = [ids componentsSeparatedByString:@","];
            parameters.debugSettings = debug;
        }
        [UMPConsentInformation.sharedInstance
            requestConsentInfoUpdateWithParameters:parameters
                                 completionHandler:^(NSError *_Nullable updateError) {
                                     if (updateError)
                                     {
                                         StudioPulseUmpReply(callback, updateError);
                                         return;
                                     }
                                     dispatch_async(dispatch_get_main_queue(), ^{
                                         if (updated) updated();
                                         [UMPConsentForm loadAndPresentIfRequiredFromViewController:UnityGetGLViewController()
                                                                                  completionHandler:^(NSError *_Nullable formError) {
                                                                                      StudioPulseUmpReply(callback, formError);
                                                                                  }];
                                     });
                                 }];
    }

    int StudioPulseUmpCanRequestAds()
    {
        return UMPConsentInformation.sharedInstance.canRequestAds ? 1 : 0;
    }

    int StudioPulseUmpPrivacyOptionsRequired()
    {
        return UMPConsentInformation.sharedInstance.privacyOptionsRequirementStatus == UMPPrivacyOptionsRequirementStatusRequired ? 1 : 0;
    }

    void StudioPulseUmpShowPrivacyOptions(StudioPulseUmpCallback callback)
    {
        dispatch_async(dispatch_get_main_queue(), ^{
            [UMPConsentForm presentPrivacyOptionsFormFromViewController:UnityGetGLViewController()
                                                      completionHandler:^(NSError *_Nullable error) {
                                                          StudioPulseUmpReply(callback, error);
                                                      }];
        });
    }
}
