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

// 2. and 3. Report what matters from your game code, e.g.:
public sealed class PulseQuickStartLevel : MonoBehaviour
{
    public int level = 1;

    void Start()
    {
        Pulse.LevelStart(level);
    }

    public void OnWin(int score, int stars)
    {
        Pulse.LevelComplete(level, score, stars);
    }

    public void OnLose(Vector3 playerWorldPos, Bounds levelBounds, float progress)
    {
        Vector2 norm = new Vector2(
            Mathf.InverseLerp(levelBounds.min.x, levelBounds.max.x, playerWorldPos.x),
            Mathf.InverseLerp(levelBounds.min.y, levelBounds.max.y, playerWorldPos.y));
        Pulse.LevelFail(level, "hit_spike", norm, progress);
    }

    public void OnMusicToggled(bool on)
    {
        Pulse.SettingChange(PulseContract.Setting.Music, !on, on);
    }
}
