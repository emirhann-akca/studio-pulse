// Pure C# (no UnityEngine): System.Net.Http transport for console apps, servers,
// tools and tests. Unity games use UnityWebRequestTransport (the adapter).
using System;
using System.Linq;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Threading.Tasks;

namespace Studio.Pulse
{
    public sealed class HttpClientTransport : IPulseTransport
    {
        readonly HttpClient _client;

        public HttpClientTransport(TimeSpan? timeout = null)
        {
            _client = new HttpClient { Timeout = timeout ?? TimeSpan.FromSeconds(20) };
        }

        public void Post(string url, string writeKey, byte[] body, int length, bool gzip, Action<PulseHttpResult> done)
        {
            var content = new ByteArrayContent(body, 0, length);
            content.Headers.ContentType = new MediaTypeHeaderValue("application/json");
            if (gzip) content.Headers.ContentEncoding.Add("gzip");
            var req = new HttpRequestMessage(HttpMethod.Post, url) { Content = content };
            req.Headers.TryAddWithoutValidation("X-Pulse-Key", writeKey);
            _client.SendAsync(req).ContinueWith(t => Finish(t, req, done), TaskScheduler.Default);
        }

        static void Finish(Task<HttpResponseMessage> t, HttpRequestMessage req, Action<PulseHttpResult> done)
        {
            var r = new PulseHttpResult { RetryAfterSeconds = -1 };
            try
            {
                if (t.IsFaulted || t.IsCanceled)
                {
                    r.Status = 0;
                    r.Error = t.IsCanceled ? "timeout" : (t.Exception?.GetBaseException().Message ?? "network error");
                }
                else
                {
                    using (HttpResponseMessage resp = t.Result)
                    {
                        r.Status = (int)resp.StatusCode;
                        if (resp.Headers.RetryAfter != null && resp.Headers.RetryAfter.Delta.HasValue)
                        {
                            r.RetryAfterSeconds = (int)resp.Headers.RetryAfter.Delta.Value.TotalSeconds;
                        }
                        else if (resp.Headers.TryGetValues("Retry-After", out var vals)
                                 && int.TryParse(vals.FirstOrDefault(), out int sec))
                        {
                            r.RetryAfterSeconds = sec;
                        }
                        r.Body = resp.Content.ReadAsStringAsync().GetAwaiter().GetResult();
                    }
                }
            }
            catch (Exception ex)
            {
                r.Status = 0;
                r.Error = ex.Message;
            }
            finally
            {
                req.Dispose();
            }
            done(r);
        }
    }
}
