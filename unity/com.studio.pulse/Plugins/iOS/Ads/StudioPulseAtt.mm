// App Tracking Transparency for Studio.Pulse.Ads (StudioAtt.cs). Included in an iOS build only
// when the project defines STUDIO_PULSE_ATT, so apps without ads do not link the ATT request.
#import <AppTrackingTransparency/AppTrackingTransparency.h>

typedef void (*StudioPulseAttCallback)(int status);

extern "C"
{
    // 0 not determined, 1 restricted, 2 denied, 3 authorized (before iOS 14: 3, no prompt exists).
    int StudioPulseAttStatus()
    {
        if (@available(iOS 14, *))
        {
            return (int)[ATTrackingManager trackingAuthorizationStatus];
        }
        return 3;
    }

    // Shows the system prompt once; the callback runs on the main thread with the answer.
    void StudioPulseAttRequest(StudioPulseAttCallback callback)
    {
        if (@available(iOS 14, *))
        {
            [ATTrackingManager requestTrackingAuthorizationWithCompletionHandler:^(ATTrackingManagerAuthorizationStatus status) {
                dispatch_async(dispatch_get_main_queue(), ^{
                    if (callback) callback((int)status);
                });
            }];
        }
        else if (callback)
        {
            callback(3);
        }
    }
}
