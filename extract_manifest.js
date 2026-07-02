// Run via: cmux browser surface:N eval "$(cat extract_manifest.js)"
// Expects to be on a Panopto Sessions/List.aspx page — folder ID read from URL hash.
//
// Returns: { folderId, count, sessionsErrored, manifest: [{
//   id, name, startTime, duration,
//   podcast,                       // primary mp4 URL on CloudFront, or null
//   streams: [{name, url}],        // secondary HLS feeds (cam/slides)
//   captions: [{language, srt}],   // SRT bodies fetched inline; empty array if none
//   error,                         // null on success; string on per-session failure
// }]}
(async () => {
  const folderId = decodeURIComponent(location.hash).match(/folderID="?([0-9a-f-]+)"?/i)?.[1];
  if (!folderId) throw new Error('No folderID in URL hash: ' + location.hash);

  const sessions = await fetch('/Panopto/Services/Data.svc/GetSessions', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      queryParameters: {
        folderID: folderId,
        page: 0,
        maxResults: 1000,
        sortAscending: true,
        sortColumn: 1,
        getFolderData: true,
      },
    }),
  }).then(r => r.json()).then(d => d.d?.Results || []);

  // Format SRT timestamp from seconds (float).
  const srtTime = (val) => {
    const h = Math.floor(val / 3600);
    const m = Math.floor((val % 3600) / 60);
    const s = Math.floor(val % 60);
    const ms = Math.floor((val - Math.floor(val)) * 1000);
    const pad = (n, w) => String(n).padStart(w, '0');
    return `${pad(h,2)}:${pad(m,2)}:${pad(s,2)},${pad(ms,3)}`;
  };

  // Fetch caption JSON for one (deliveryId, language) and return SRT string.
  // Returns '' if Panopto returns no captions or an error.
  const fetchCaptions = async (deliveryId, language) => {
    try {
      const data = await fetch('/Panopto/Pages/Viewer/DeliveryInfo.aspx', {
        method: 'POST',
        headers: { 'content-type': 'application/x-www-form-urlencoded;charset=UTF-8' },
        body: `deliveryId=${deliveryId}&getCaptions=true&language=${language}&responseType=json`,
      }).then(r => r.json());
      if (!Array.isArray(data) || data.length === 0) return '';
      let out = '';
      let i = 1;
      for (const c of data) {
        if (!c.Caption) continue;
        const start = c.Time;
        const end = start + c.CaptionDuration;
        out += `${i}\n${srtTime(start)} --> ${srtTime(end)}\n${c.Caption}\n\n`;
        i++;
      }
      return out;
    } catch {
      return '';
    }
  };

  // Cap concurrency so we don't hammer Panopto on large folders.
  const limit = 6;
  const manifest = new Array(sessions.length);
  let cursor = 0;
  const worker = async () => {
    while (cursor < sessions.length) {
      const idx = cursor++;
      const s = sessions[idx];
      try {
        const di = await fetch('/Panopto/Pages/Viewer/DeliveryInfo.aspx', {
          method: 'POST',
          headers: { 'content-type': 'application/x-www-form-urlencoded;charset=UTF-8' },
          body: `deliveryId=${s.DeliveryID}&isEmbed=true&responseType=json`,
        }).then(r => r.json());
        const langs = (di.Delivery?.AvailableCaptions || []).map(c => c.Language);
        const captions = [];
        for (const lang of langs) {
          const srt = await fetchCaptions(s.DeliveryID, lang);
          if (srt) captions.push({ language: String(lang), srt });
        }
        manifest[idx] = {
          id: s.DeliveryID,
          name: s.SessionName,
          startTime: s.StartTime,
          duration: di.Delivery?.Duration ?? 0,
          podcast: di.Delivery?.PodcastStreams?.[0]?.StreamUrl ?? null,
          streams: (di.Delivery?.Streams || []).map(x => ({ name: x.Name, url: x.StreamUrl })),
          captions,
          error: di.ErrorCode ? String(di.ErrorMessage || di.ErrorCode) : null,
        };
      } catch (e) {
        manifest[idx] = { id: s.DeliveryID, name: s.SessionName, error: String(e) };
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, sessions.length) }, worker));

  const sessionsErrored = manifest.filter(m => m && m.error).length;
  return { folderId, count: manifest.length, sessionsErrored, manifest };
})()
