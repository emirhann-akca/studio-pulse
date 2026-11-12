// 2. and 3. Report what matters from your game code (attach to the level's root object).
using Studio.Pulse;
using UnityEngine;

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
