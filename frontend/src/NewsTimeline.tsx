import { useEffect, useState } from "react";

interface NewsEvent {
  source: string;
  url: string;
  title: string;
  published_at: string;
  received_at: string;
}

const fedSource = "federal-reserve-monetary-policy";
const fedLink = /^https:\/\/www\.federalreserve\.gov\/newsevents\/pressreleases\/monetary[0-9]{8}[a-z]+\.htm$/;
const dateFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

type Timeline =
  | { state: "loading" | "unavailable" }
  | { state: "ready"; events: NewsEvent[] };

export function NewsTimeline() {
  const [timeline, setTimeline] = useState<Timeline>({ state: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(5000)]);
    async function load() {
      try {
        const response = await fetch("/api/v1/news/events?limit=10", {
          signal,
          cache: "no-store",
        });
        if (!response.ok) throw new Error("News unavailable");
        const events = (await response.json()) as NewsEvent[];
        if (!controller.signal.aborted) setTimeline({ state: "ready", events });
      } catch {
        if (!controller.signal.aborted) setTimeline({ state: "unavailable" });
      }
    }
    void load();
    return () => controller.abort();
  }, []);

  return (
    <section className="panel" aria-labelledby="news-heading">
      <h2 id="news-heading">Monetary policy releases</h2>
      <div role="status">
        {timeline.state === "loading" && <p>Loading saved releases…</p>}
        {timeline.state === "unavailable" && <p>Saved releases are unavailable.</p>}
        {timeline.state === "ready" && timeline.events.length === 0 && (
          <p>No releases saved. Run <code>hcs-trader import-news</code> to update the timeline.</p>
        )}
      </div>
      {timeline.state === "ready" && timeline.events.length > 0 && (
        <ul className="news-list">
          {timeline.events.map((event) => {
            const isFed = event.source === fedSource && fedLink.test(event.url);
            return (
              <li key={event.source + event.url}>
                <h3>{isFed ? <a href={event.url}>{event.title}</a> : event.title}</h3>
                <p>{isFed ? "Federal Reserve Board" : event.source}</p>
                <p>
                  Published <time dateTime={event.published_at}>{dateFormat.format(new Date(event.published_at))}</time>
                  {" · Received "}
                  <time dateTime={event.received_at}>{dateFormat.format(new Date(event.received_at))}</time>
                </p>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
