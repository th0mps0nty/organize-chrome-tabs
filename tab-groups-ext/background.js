
chrome.runtime.onInstalled.addListener(async () => {
  const wins = await chrome.windows.getAll({ populate: true });
  let groups = 0;
  for (const win of wins) {
    const byDomain = {};
    for (const tab of win.tabs) {
      if (!tab.url || tab.url.startsWith("chrome")) continue;
      let domain = "other";
      try { domain = new URL(tab.url).hostname.replace(/^www\./, ""); } catch {}
      (byDomain[domain] = byDomain[domain] || []).push(tab.id);
    }
    for (const [domain, ids] of Object.entries(byDomain)) {
      if (ids.length < 2) continue;
      try {
        const gid = await chrome.tabs.group({ tabIds: ids, createProperties: { windowId: win.id } });
        await chrome.tabGroups.update(gid, { title: domain, collapsed: ids.length > 6 });
        groups++;
      } catch (e) { console.warn("Could not group", domain, e.message); }
    }
  }
  console.log(`Tab Group Organizer: created ${groups} group(s).`);
});
