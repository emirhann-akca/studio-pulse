using Studio.Pulse.Ads;
using UnityEngine;

/// <summary>
/// Starts whatever Resources/studio_app.json turns on (analytics, ads, MMP) and shows how a game
/// asks for ads. Put it on an object in the first scene. With an empty config nothing starts and
/// RewardedReady stays false, so the game simply hides its ad buttons.
/// </summary>
public sealed class StudioAdsQuickStart : MonoBehaviour
{
    void Awake()
    {
        StudioApp.Start();
        StudioAds.AvailabilityChanged += () => Debug.Log("rewarded ready: " + StudioAds.RewardedReady);
    }

    /// <summary>Call from a "Watch an ad" button.</summary>
    public void WatchAdForCoins()
    {
        StudioAds.ShowRewarded("coins", granted =>
        {
            if (granted) Debug.Log("give the coins here");
        });
    }

    /// <summary>Call at a natural break (after a level's result screen).</summary>
    public void LevelEnded()
    {
        StudioAds.ShowInterstitial("level_end", () => Debug.Log("continue the game here"));
    }
}
