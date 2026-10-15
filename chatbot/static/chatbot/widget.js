(function () {
  // Prevent duplicate initialization
  if (window.__flipkartAssistantLoaded) return;
  window.__flipkartAssistantLoaded = true;

  var script = document.currentScript || document.querySelector('script[data-store-key]') || document.querySelector('script[src*="widget.js"]');
  var storeKey = (script && script.getAttribute("data-store-key")) || "Lref8GVYiVxxZ4wehiudiZRlAcI5myX-QG48E4K33V0";
  var apiBase = "http://127.0.0.1:8000";
  if (script && script.src) {
    try {
      var parsed = new URL(script.src, window.location.href);
      if (parsed.protocol.startsWith("http")) {
        apiBase = parsed.origin;
      }
    } catch (e) {}
  }

  // chat_sid identifies this browser. chat_cid is the conversation currently open.
  var sid;
  try {
    sid = localStorage.getItem("chat_sid");
    if (!sid) {
      sid = "s" + Math.random().toString(36).slice(2) + Date.now();
      localStorage.setItem("chat_sid", sid);
    }
  } catch (e) {
    sid = "s" + Math.random().toString(36).slice(2);
  }

  var cid = null;
  try { cid = localStorage.getItem("chat_cid") || null; } catch (e) {}
  function setCid(v) {
    cid = v || null;
    try {
      if (cid) localStorage.setItem("chat_cid", cid);
      else localStorage.removeItem("chat_cid");
    } catch (e) {}
  }

  function setOpenFlag(v) {
    try { localStorage.setItem("chat_open", v ? "1" : "0"); } catch (e) {}
  }
  function getOpenFlag() {
    try { return localStorage.getItem("chat_open") === "1"; } catch (e) { return false; }
  }

  function newRequestId() {
    return "r" + Math.random().toString(36).slice(2) + Date.now().toString(36);
  }

  function el(tag, css, text, id) {
    var e = document.createElement(tag);
    if (id) e.id = id;
    if (css) e.style.cssText = css;
    if (text) e.textContent = text;
    return e;
  }

  function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ "X-Store-Key": storeKey }, opts.headers || {});
    return fetch(apiBase + path, opts).then(function (r) {
      if (!r.ok) throw new Error("Server returned status " + r.status);
      return r.json();
    });
  }

  // Floating trigger button (Flipkart Blue with glow)
  var button = el("button", "position:fixed;bottom:24px;right:24px;width:60px;height:60px;border-radius:50%;border:0;background:#2874f0;color:#fff;font-size:26px;cursor:pointer;z-index:999999;box-shadow:0 4px 20px rgba(40,116,240,0.45);display:flex;align-items:center;justify-content:center;transition:transform .2s ease", "💬", "fk-chat-toggle");
  button.title = "Chat with Flipkart Shopping Assistant";
  button.onmouseenter = function () { button.style.transform = "scale(1.08)"; };
  button.onmouseleave = function () { button.style.transform = "scale(1)"; };

  // Chat window container (header on top, sidebar + conversation below)
  var box = el("div", "position:fixed;bottom:96px;right:24px;width:640px;height:540px;max-height:calc(100vh - 120px);background:#fff;border-radius:16px;display:none;flex-direction:column;z-index:999999;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;box-shadow:0 12px 32px rgba(0,0,0,0.18);overflow:hidden;border:1px solid #e0e0e0", "", "fk-chat-box");

  // Header
  var header = el("div", "background:#2874f0;color:#fff;padding:12px 16px;display:flex;align-items:center;justify-content:space-between;box-shadow:0 2px 4px rgba(0,0,0,0.08)");
  var headerLeft = el("div", "display:flex;align-items:center;gap:10px");
  var sideToggle = el("button", "background:transparent;border:0;color:#fff;font-size:20px;cursor:pointer;padding:2px 6px;display:none", "☰");
  sideToggle.title = "Chats";
  var avatar = el("div", "width:34px;height:34px;background:#ffe500;color:#2874f0;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:900;font-size:18px", "✦");
  var titleBox = el("div");
  var title = el("div", "font-weight:700;font-size:15px;letter-spacing:0.2px", "Flipkart Assistant");
  var sub = el("div", "font-size:11px;color:#dbeafe;display:flex;align-items:center;gap:4px");
  var dot = el("span", "width:6px;height:6px;background:#4ade80;border-radius:50%;display:inline-block");
  sub.appendChild(dot);
  sub.appendChild(document.createTextNode("Online • Flipkart AI Guide"));
  titleBox.appendChild(title);
  titleBox.appendChild(sub);
  headerLeft.appendChild(sideToggle);
  headerLeft.appendChild(avatar);
  headerLeft.appendChild(titleBox);

  var closeBtn = el("button", "background:transparent;border:0;color:#fff;font-size:20px;cursor:pointer;padding:4px", "✕");
  closeBtn.onclick = function () { window.closeFlipkartAssistant(); };
  header.appendChild(headerLeft);
  header.appendChild(closeBtn);

  // Body: sidebar on the left, conversation on the right
  var body = el("div", "flex:1;display:flex;min-height:0;position:relative");

  var sidebar = el("div", "width:200px;flex-shrink:0;background:#f1f5f9;border-right:1px solid #e2e8f0;display:flex;flex-direction:column;z-index:5");
  var newBtn = el("button", "margin:10px 10px 4px 10px;padding:9px 10px;border:1px solid #2874f0;background:#2874f0;color:#fff;border-radius:8px;font-size:12.5px;font-weight:700;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:4px", "+ New chat");
  var resetSearchBtn = el("button", "margin:4px 10px 10px 10px;padding:6px 10px;border:1px solid #cbd5e1;background:#fff;color:#475569;border-radius:8px;font-size:11.5px;font-weight:600;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:4px", "↺ Reset search");
  resetSearchBtn.title = "Clear current search filters while keeping message history";
  var sideLabel = el("div", "font-size:11px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:0.5px;padding:2px 12px 6px", "Your chats");
  var sideList = el("div", "flex:1;overflow-y:auto;padding:0 8px 8px");
  sidebar.appendChild(newBtn);
  sidebar.appendChild(resetSearchBtn);
  sidebar.appendChild(sideLabel);
  sidebar.appendChild(sideList);

  var main = el("div", "flex:1;display:flex;flex-direction:column;min-width:0");

  // Message list
  var list = el("div", "flex:1;overflow-y:auto;padding:14px;font-size:13px;line-height:1.5;background:#f8f9fa", "", "fk-chat-list");

  // Chips
  var chips = el("div", "display:flex;flex-wrap:wrap;gap:6px;margin:8px 0");
  var sampleQueries = [
    "Running shoes under ₹2,000",
    "Show me only Nike shoes",
    "Laptops under ₹50,000",
    "Compare boAt and Sony headphones"
  ];
  sampleQueries.forEach(function (q) {
    var chip = el("button", "background:#fff;border:1px solid #2874f0;color:#2874f0;padding:5px 10px;border-radius:14px;font-size:11px;cursor:pointer;font-weight:500;transition:all .2s");
    chip.textContent = q;
    chip.onclick = function () {
      window.sendFlipkartChat(q);
    };
    chips.appendChild(chip);
  });

  // Input row
  var row = el("div", "display:flex;border-top:1px solid #e5e7eb;background:#fff;padding:8px");
  var input = el("input", "flex:1;border:1px solid #e5e7eb;border-radius:20px;padding:10px 14px;font-size:13px;outline:none;background:#f9fafb;min-width:0", "", "fk-chat-input");
  input.placeholder = "Ask about shoes, mobiles, laptops...";
  var send = el("button", "border:0;background:#2874f0;color:#fff;padding:0 16px;border-radius:20px;margin-left:8px;cursor:pointer;font-weight:600;font-size:13px", "Send", "fk-chat-send");
  row.appendChild(input); row.appendChild(send);

  main.appendChild(list);
  main.appendChild(row);
  body.appendChild(sidebar);
  body.appendChild(main);
  box.appendChild(header);
  box.appendChild(body);
  document.body.appendChild(box);
  document.body.appendChild(button);

  // ---------------------------------------------------------------
  // Layout: sidebar always visible on wide screens, a drawer on phones
  // ---------------------------------------------------------------

  var sideOpen = false;
  function isNarrow() { return window.innerWidth < 720; }

  function applyLayout() {
    if (isNarrow()) {
      box.style.width = "calc(100vw - 24px)";
      box.style.maxWidth = "440px";
      box.style.right = "12px";
      sidebar.style.position = "absolute";
      sidebar.style.top = "0";
      sidebar.style.bottom = "0";
      sidebar.style.left = "0";
      sidebar.style.boxShadow = "4px 0 12px rgba(0,0,0,0.12)";
      sidebar.style.display = sideOpen ? "flex" : "none";
      sideToggle.style.display = "inline-block";
    } else {
      box.style.width = "640px";
      box.style.maxWidth = "none";
      box.style.right = "24px";
      sidebar.style.position = "static";
      sidebar.style.boxShadow = "none";
      sidebar.style.display = "flex";
      sideToggle.style.display = "none";
    }
  }
  sideToggle.onclick = function () { sideOpen = !sideOpen; applyLayout(); };
  window.addEventListener("resize", applyLayout);
  applyLayout();

  function closeDrawer() {
    if (isNarrow()) { sideOpen = false; applyLayout(); }
  }

  // ---------------------------------------------------------------
  // Messages and product cards
  // ---------------------------------------------------------------

  function formatPrice(p) {
    var num = Number(p.price) || 0;
    if (p.currency === "INR" || p.currency === "₹") {
      return "₹" + num.toLocaleString("en-IN");
    }
    return (p.currency || "$") + " " + num.toFixed(2);
  }

  function addMsg(text, mine) {
    var m = el("div", "margin:8px 0;padding:10px 14px;border-radius:14px;max-width:85%;white-space:pre-wrap;font-size:13px;" +
      (mine
        ? "background:#2874f0;color:#fff;margin-left:auto;border-bottom-right-radius:2px;box-shadow:0 1px 3px rgba(40,116,240,0.2)"
        : "background:#fff;color:#1e293b;border:1px solid #e2e8f0;border-bottom-left-radius:2px;box-shadow:0 1px 2px rgba(0,0,0,0.04)"),
      text);
    list.appendChild(m);
    list.scrollTop = list.scrollHeight;
    return m;
  }

  function addCards(products) {
    products.forEach(function (p) {
      var card = el("div", "display:flex;gap:10px;background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:10px;margin:8px 0;align-items:center;box-shadow:0 1px 3px rgba(0,0,0,0.05)");
      var imgSrc = p.image || p.image_url;
      if (imgSrc) {
        var imgBox = el("div", "width:60px;height:60px;flex-shrink:0;background:#f8f9fa;border-radius:6px;display:flex;align-items:center;justify-content:center;overflow:hidden;border:1px solid #f0f0f0");
        var img = el("img", "width:100%;height:100%;object-fit:contain");
        img.src = imgSrc;
        img.referrerPolicy = "no-referrer";
        img.onerror = function() {
          imgBox.innerHTML = "<div style='font-size:26px;'>🛍️</div>";
        };
        imgBox.appendChild(img);
        card.appendChild(imgBox);
      }
      var info = el("div", "flex:1;min-width:0");
      var pName = el("div", "font-weight:600;font-size:12.5px;color:#111827;white-space:nowrap;overflow:hidden;text-overflow:ellipsis", p.name);
      info.appendChild(pName);

      var priceRow = el("div", "display:flex;align-items:center;gap:6px;margin:4px 0");
      var pPrice = el("span", "font-weight:700;color:#388e3c;font-size:13.5px", formatPrice(p));
      priceRow.appendChild(pPrice);

      if (!p.in_stock) {
        var oos = el("span", "font-size:10px;color:#dc2626;background:#fee2e2;padding:2px 6px;border-radius:4px;font-weight:600", "Out of Stock");
        priceRow.appendChild(oos);
      } else {
        var inStock = el("span", "font-size:10px;color:#16a34a;background:#dcfce7;padding:2px 6px;border-radius:4px;font-weight:600", "In Stock");
        priceRow.appendChild(inStock);
      }
      info.appendChild(priceRow);

      if (p.url) {
        var btn = el("a", "display:inline-block;background:#fb641b;color:#fff;font-size:11px;font-weight:700;padding:4px 10px;border-radius:4px;text-decoration:none;margin-top:2px;cursor:pointer", "View on Flipkart ↗");
        btn.href = p.url;
        btn.target = "_blank";
        btn.rel = "noopener noreferrer";
        info.appendChild(btn);
      }
      card.appendChild(info);
      list.appendChild(card);
    });
    list.scrollTop = list.scrollHeight;
  }

  // ---------------------------------------------------------------
  // Conversation: welcome, restore, switch, new chat
  // ---------------------------------------------------------------

  function renderWelcome() {
    list.innerHTML = "";
    addMsg("Namaste! Welcome to Flipkart. I am your shopping assistant. How can I help you find products today?", false);
    list.appendChild(chips);
  }

  function renderHistory(msgs) {
    list.innerHTML = "";
    msgs.forEach(function (m) {
      addMsg(m.content, m.role === "user");
      if (m.role !== "user" && m.products && m.products.length) addCards(m.products);
    });
    list.scrollTop = list.scrollHeight;
  }

  function fmtDate(iso) {
    try {
      var d = new Date(iso);
      return d.toLocaleDateString(undefined, { day: "numeric", month: "short" }) + ", " +
             d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    } catch (e) { return ""; }
  }

  // Draws the left sidebar from the server's list of this browser's chats.
  function refreshSidebar() {
    return api("/api/conversations/?session_id=" + encodeURIComponent(sid)).then(function (d) {
      var items = d.conversations || [];
      sideList.innerHTML = "";

      // A brand-new chat has no messages yet, so show it as a placeholder at the top.
      var known = items.some(function (c) { return c.id === cid; });
      if (cid && !known) {
        sideList.appendChild(sideItem({ id: cid, title: "New chat", updated: "" }, true));
      }
      items.forEach(function (c) { sideList.appendChild(sideItem(c, c.id === cid)); });

      if (!items.length && !(cid && !known)) {
        sideList.appendChild(el("div", "font-size:12px;color:#64748b;padding:8px 6px", "No chats yet."));
      }
    }).catch(function () {
      sideList.innerHTML = "";
      sideList.appendChild(el("div", "font-size:12px;color:#64748b;padding:8px 6px", "Could not load chats."));
    });
  }

  function sideItem(c, active) {
    var it = el("div", "padding:9px 10px;border-radius:8px;margin-bottom:4px;cursor:pointer;border:1px solid " +
      (active ? "#2874f0" : "transparent") + ";background:" + (active ? "#dbeafe" : "transparent"));
    it.appendChild(el("div", "font-size:12.5px;font-weight:600;color:#0f172a;white-space:nowrap;overflow:hidden;text-overflow:ellipsis", c.title || "New chat"));
    if (c.updated) it.appendChild(el("div", "font-size:10.5px;color:#64748b;margin-top:2px", fmtDate(c.updated)));
    it.onmouseenter = function () { if (!active) it.style.background = "#e2e8f0"; };
    it.onmouseleave = function () { if (!active) it.style.background = "transparent"; };
    it.onclick = function () {
      if (send.disabled || c.id === cid) { closeDrawer(); return; }
      closeDrawer();
      loadConversation(c.id);
    };
    return it;
  }

  // Loads a conversation (or this browser's latest one when no id is given).
  function loadConversation(wantedCid) {
    var url = "/api/history/?session_id=" + encodeURIComponent(sid) +
              (wantedCid ? "&conversation_id=" + encodeURIComponent(wantedCid) : "");
    return api(url).then(function (d) {
      setCid(d.conversation_id);
      if (d.messages && d.messages.length) renderHistory(d.messages);
      else renderWelcome();
    }).catch(function (err) {
      console.error("[Flipkart Assistant] could not load history:", err);
      renderWelcome();
    }).then(function () {
      return refreshSidebar();
    });
  }

  function startNewChat() {
    if (send.disabled) return;           // a reply is still being written
    closeDrawer();
    api("/api/conversations/new/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sid })
    }).then(function (d) {
      setCid(d.conversation_id);
      renderWelcome();
      refreshSidebar();
      input.focus();
    }).catch(function (err) {
      console.error("[Flipkart Assistant] new chat failed:", err);
      addMsg("Could not start a new chat. Please try again.", false);
    });
  }
  newBtn.onclick = startNewChat;

  resetSearchBtn.onclick = function () {
    if (send.disabled || !cid) return;
    closeDrawer();
    api("/api/conversations/reset/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sid, conversation_id: cid })
    }).then(function (d) {
      addMsg("Shopping search context cleared. What would you like to search for now?", false);
    }).catch(function (err) {
      console.error("[Flipkart Assistant] reset search failed:", err);
    });
  };

  // ---------------------------------------------------------------
  // Sending
  // ---------------------------------------------------------------

  var loaded = false;
  var ready = Promise.resolve();

  function sendMessage() {
    var text = input.value.trim();
    if (!text) return;
    input.value = "";
    closeDrawer();
    addMsg(text, true);
    var wait = addMsg("Checking Flipkart catalogue...", false);
    send.disabled = true;
    input.disabled = true;

    fetch(apiBase + "/api/chat/", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Store-Key": storeKey },
      body: JSON.stringify({
        message: text,
        session_id: sid,
        conversation_id: cid,
        request_id: newRequestId()
      })
    }).then(function (r) {
      if (!r.ok) {
        return r.json().then(function(errData) {
          throw new Error(errData.detail || errData.error || ("Server returned status " + r.status));
        }).catch(function(e) {
          throw new Error("Server returned status " + r.status);
        });
      }
      return r.json();
    }).then(function (d) {
      if (d.conversation_id) setCid(d.conversation_id);
      wait.textContent = d.reply || d.error || "Sorry, something went wrong.";
      if (d.products && d.products.length) {
        addCards(d.products);
      }
      refreshSidebar();                  // the chat's title appears after its first message
    }).catch(function (err) {
      console.error("[Flipkart Assistant Error]:", err);
      wait.textContent = "Unable to connect: " + (err.message || "Please check if Django server is running.");
    }).finally(function () {
      send.disabled = false;
      input.disabled = false;
      input.focus();
    });
  }

  send.onclick = function () { ready.then(sendMessage); };
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") ready.then(sendMessage);
  });

  // ---------------------------------------------------------------
  // Open / close (the open state also survives a page refresh)
  // ---------------------------------------------------------------

  window.openFlipkartAssistant = function () {
    box.style.display = "flex";
    setOpenFlag(true);
    applyLayout();
    if (!loaded) {
      loaded = true;
      ready = loadConversation(cid);      // restores the earlier chat, or shows the welcome
    }
    setTimeout(function() { input.focus(); }, 100);
    return ready;
  };

  window.closeFlipkartAssistant = function () {
    box.style.display = "none";
    setOpenFlag(false);
  };

  window.toggleFlipkartAssistant = function () {
    if (box.style.display === "none") window.openFlipkartAssistant();
    else window.closeFlipkartAssistant();
  };

  window.sendFlipkartChat = function (text) {
    window.openFlipkartAssistant().then(function () {
      input.value = text;
      sendMessage();
    });
  };

  button.onclick = window.toggleFlipkartAssistant;

  // After a refresh, reopen the chat if it was open before.
  if (getOpenFlag()) window.openFlipkartAssistant();
})();