using System.Collections.Generic;

namespace Studio.Pulse
{
    /// <summary>
    /// Receives batches of events on the main thread (every flush interval and
    /// on pause/quit). Implementations must not hold on to the list.
    /// </summary>
    public interface IPulseSink
    {
        void Send(IReadOnlyList<PulseEvent> batch, PulseContext context);

        /// <summary>Called when the app pauses or quits; finish pending work quickly.</summary>
        void Flush();
    }
}
