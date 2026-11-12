// Studio Pulse quick start: the three lines a game needs (docs/SDK_UNITY.md).
using Studio.Pulse;
using UnityEngine;

public static class PulseQuickStart
{
    // 1. Start the SDK before the first scene loads.
    [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
    static void Boot()
    {
        Pulse.Init("sp_pub_local", "http://127.0.0.1:8787"); // your write key and collector URL
    }
}
