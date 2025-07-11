// ==UserScript==
// @name         LMArena Automator
// @namespace    http://tampermonkey.net/
// @version      7.1
// @description  Injects history with robust, buffered comprehensive logging and error reporting.
// @author       Lianues
// @updateURL    https://raw.githubusercontent.com/Lianues/LMArenaBridge/main/TampermonkeyScript/LMArenaAutomator.js
// @downloadURL  https://raw.githubusercontent.com/Lianues/LMArenaBridge/main/TampermonkeyScript/LMArenaAutomator.js
// @match        https://lmarena.ai/c/*
// @match        https://canary.lmarena.ai/c/*
// @grant        none
// @run-at       document-start
// ==/UserScript==

(function() {
    'use strict';

    const SERVER_URL = 'http://127.0.0.1:5102';
    let config = {};
    // --- Robust Tab ID Management ---
    const TAB_REGISTRY_KEY = 'lmarena_automator_tab_registry';

    function getTabRegistry() {
        try {
            return JSON.parse(localStorage.getItem(TAB_REGISTRY_KEY)) || {};
        } catch {
            return {};
        }
    }

    function setTabRegistry(registry) {
        localStorage.setItem(TAB_REGISTRY_KEY, JSON.stringify(registry));
    }

    function initializeTabId() {
        let currentTabId = sessionStorage.getItem('lmarena_automator_tab_id');
        const registry = getTabRegistry();
        const now = Date.now();

        // Check if the current ID is already actively used by another tab
        if (currentTabId && registry[currentTabId] && (now - registry[currentTabId] < 4000)) {
            console.warn(`[Tab ${currentTabId.substring(0, 4)}] Duplicate tab detected. Generating new ID.`);
            currentTabId = null; // Force new ID generation
        }

        if (!currentTabId) {
            currentTabId = crypto.randomUUID();
            sessionStorage.setItem('lmarena_automator_tab_id', currentTabId);
        }
        
        return currentTabId;
    }

    const tabId = initializeTabId();

    function heartbeat() {
        const registry = getTabRegistry();
        const now = Date.now();
        registry[tabId] = now;

        // Clean up stale entries older than 10 seconds
        for (const id in registry) {
            if (now - registry[id] > 10000) {
                delete registry[id];
            }
        }
        setTabRegistry(registry);
    }
    
    // Register tab on load and then periodically
    heartbeat();
    setInterval(heartbeat, 2000);

    window.addEventListener('beforeunload', () => {
        const registry = getTabRegistry();
        delete registry[tabId];
        setTabRegistry(registry);
    });
    // --- End of Tab ID Management ---
    const originalConsole = {
        log: console.log.bind(console),
        warn: console.warn.bind(console),
        error: console.error.bind(console),
    };

    let logBuffer = [];
    let configLoaded = false;
    let comprehensiveLoggingEnabled = false;

    async function sendLogToServer(level, message) {
        try {
            await fetch(`${SERVER_URL}/log_from_client?tab_id=${tabId}`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ level, message, tab_id: tabId })
            });
        } catch (e) {
            originalConsole.error("[Automator] Failed to send log to server:", e);
        }
    }

    function handleLog(level, args) {
        originalConsole[level.toLowerCase()]?.(...args);
        const message = args.map(arg => {
            try { return (typeof arg === 'object' && arg !== null) ? JSON.stringify(arg, null, 2) : String(arg); }
            catch (e) { return "[[Unserializable Object]]"; }
        }).join(' ');
        if (configLoaded && comprehensiveLoggingEnabled) {
            sendLogToServer(level, message);
        } else {
            logBuffer.push({ level, message });
        }
    }

    console.log = (...args) => handleLog('LOG', args);
    console.warn = (...args) => handleLog('WARN', args);
    console.error = (...args) => handleLog('ERROR', args);

    console.log("LMArena Automator v7.1 (Final Logging Fix): Script started...");

    async function loadConfig() {
        console.log(`[Tab ${tabId.substring(0, 4)}] Attempting to load config from server...`);
        try {
            const response = await fetch(`${SERVER_URL}/get_config?tab_id=${tabId}`);
            if (response.ok) {
                config = await response.json();
                comprehensiveLoggingEnabled = !!config.enable_comprehensive_logging;
                console.log("Config loaded. Comprehensive logging is", comprehensiveLoggingEnabled ? "ENABLED." : "DISABLED.");
            } else {
                console.error(`Failed to load config, server returned status: ${response.status}`);
            }
        } catch (e) {
            console.error("Critical error fetching config:", e);
        } finally {
            configLoaded = true;
            if (comprehensiveLoggingEnabled) {
                console.log(`Processing ${logBuffer.length} buffered logs...`);
                logBuffer.forEach(log => sendLogToServer(log.level, log.message));
                logBuffer = [];
            }
        }
    }

    function hookFetch() {
        const originalFetch = window.fetch;
        window.fetch = async function(...args) {
            const url = args[0] instanceof Request ? args[0].url : args[0];
            const originalOptions = args[1] || {};

            if (typeof url === 'string' && url.includes('/api/stream/post-to-evaluation/')) {
                let bodyObject;
                try { bodyObject = JSON.parse(originalOptions.body); } catch (e) { return originalFetch.apply(this, args); }

                const lastUserMessage = (bodyObject.messages || []).slice().reverse().find(m => m.role === 'user');

                if (lastUserMessage && lastUserMessage.content.startsWith('[这条消息仅起占位，请以外部应用中显示的内容为准：/')) {
                    console.log('LMArena Automator: Trigger detected. Performing stateful merge...');
                    // 无条件记录调试信息，由日志总开关决定是否发送
                    console.log('Automator Debug: Original body:', bodyObject);
                    
                    try {
                        const serverResponse = await fetch(`${SERVER_URL}/get_messages_job?tab_id=${tabId}`);
                        const data = await serverResponse.json();

                        if (data.status === 'success' && data.job) {
                            const { message_templates, target_model_id, task_id } = data.job; // 拿到 task_id
                            sessionStorage.setItem('current_task_id', task_id); // 存储 task_id
                            const lastOriginalMessage = bodyObject.messages[bodyObject.messages.length - 1];
                            const { evaluationId, evaluationSessionId } = lastOriginalMessage;

                            let parentMessageId = null;
                            const newMessages = message_templates.map(template => {
                                const newMsg = {
                                    ...template, id: crypto.randomUUID(), evaluationId, evaluationSessionId,
                                    parentMessageIds: parentMessageId ? [parentMessageId] : [],
                                    experimental_attachments: [], failureReason: null, metadata: null,
                                    participantPosition: "a", createdAt: new Date().toISOString(),
                                    updatedAt: new Date().toISOString(),
                                    status: template.role === 'assistant' ? 'pending' : 'success',
                                };
                                parentMessageId = newMsg.id;
                                return newMsg;
                            });

                            bodyObject.messages = newMessages;
                            bodyObject.modelAId = target_model_id;
                            const finalUserMessage = newMessages.slice().reverse().find(m => m.role === 'user');
                            const finalAssistantMessage = newMessages.slice().reverse().find(m => m.role === 'assistant');
                            if(finalUserMessage) bodyObject.userMessageId = finalUserMessage.id;
                            if(finalAssistantMessage) bodyObject.modelAMessageId = finalAssistantMessage.id;
                            
                            if (config.log_tampermonkey_debug) console.log('Merged body:', bodyObject);

                            const newOptions = { ...originalOptions, body: JSON.stringify(bodyObject) };
                            const response = await originalFetch.apply(this, [url, newOptions]);
                            handleResponseStream(response);
                            return response;
                        }
                    } catch (e) {
                        console.error("LMArena Automator: Failed to perform stateful merge:", e);
                    }
                }
            }
            return originalFetch.apply(this, args);
        };
    }

    function handleResponseStream(response) {
        const taskId = sessionStorage.getItem('current_task_id');
        if (!taskId) return;
        const responseClone = response.clone();
        (async () => {
            const reader = responseClone.body.getReader();
            const decoder = new TextDecoder();
            while (true) {
                try {
                    const { done, value } = await reader.read();
                    if (done) {
                        await fetch(`${SERVER_URL}/report_result`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ task_id: taskId, tab_id: tabId, status: 'completed' }) });
                        sessionStorage.removeItem('current_task_id');
                        break;
                    }
                    const chunk = decoder.decode(value, {stream: true});
                    await fetch(`${SERVER_URL}/stream_chunk`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ task_id: taskId, tab_id: tabId, chunk: chunk }) });
                } catch (e) {
                    console.error("Error in response stream handling:", e);
                    sessionStorage.removeItem('current_task_id');
                    break;
                }
            }
        })();
    }

    async function typeAndSubmitPrompt(promptText) {
        const textarea = document.querySelector('textarea[name="text"]');
        if (!textarea) {
            console.error("Automator Error: Textarea element not found.");
            return;
        }
        const submitButton = document.querySelector('button[type="submit"]');
        if (!submitButton) {
            console.error("Automator Error: Submit button not found.");
            return;
        }
        Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set.call(textarea, promptText);
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
        await new Promise(resolve => setTimeout(resolve, 50));
        if (!submitButton.disabled) {
            submitButton.click();
        } else {
            console.warn("Automator Warning: Submit button was disabled on first attempt. Retrying might be needed if submission fails.");
        }
    }

    function connectEventSource() {
        console.log(`[Tab ${tabId.substring(0, 4)}] Connecting to SSE endpoint...`);
        const eventSource = new EventSource(`${SERVER_URL}/events?tab_id=${tabId}`);

        eventSource.onopen = () => {
            console.log(`[Tab ${tabId.substring(0, 4)}] SSE connection established.`);
        };

        eventSource.addEventListener('new_job', async (event) => {
            console.log(`[Tab ${tabId.substring(0, 4)}] New job received via SSE:`, event.data);
            const job = JSON.parse(event.data);
            
            // Ensure this tab is not already busy
            if (sessionStorage.getItem('current_task_id')) {
                console.warn(`[Tab ${tabId.substring(0, 4)}] Received a job but is already busy. Ignoring.`);
                return;
            }

            sessionStorage.setItem('current_task_id', job.task_id);
            if (job.type === 'prompt') {
                await typeAndSubmitPrompt(job.prompt);
            } else if (job.type === 'messages') {
                console.log(`[Tab ${tabId.substring(0, 4)}] Handling 'messages' job. Waiting for trigger...`);
            }
        });
        
        eventSource.addEventListener('close', () => {
            console.log(`[Tab ${tabId.substring(0, 4)}] Server requested to close SSE connection.`);
            eventSource.close();
        });

        eventSource.onerror = (err) => {
            console.error(`[Tab ${tabId.substring(0, 4)}] SSE connection error:`, err);
            eventSource.close();
            // Attempt to reconnect after a delay
            setTimeout(connectEventSource, 5000);
        };
    }

    async function sendPageSource() {
        // 等待页面完全加载
        if (document.readyState === "complete") {
            console.log(`[Tab ${tabId.substring(0, 4)}] Page loaded. Sending source to server for model check...`);
            try {
                const htmlContent = document.documentElement.outerHTML;
                await fetch(`${SERVER_URL}/update_models?tab_id=${tabId}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'text/html' },
                    body: htmlContent
                });
            } catch (e) {
                console.error("[Automator] Failed to send page source to server:", e);
            }
        }
    }

    async function main() {
        // Clear any stale task ID on script start
        sessionStorage.removeItem('current_task_id');

        await loadConfig();
        hookFetch();

        // 先处理一次性的页面加载任务
        if (document.readyState === "complete") {
            await sendPageSource();
        } else {
            // 使用 aysnc/await 确保 sendPageSource 在 load 事件后完成
            await new Promise(resolve => {
                window.addEventListener('load', async () => {
                    await sendPageSource();
                    resolve();
                });
            });
        }

        // 在所有一次性任务完成后，再建立持久的SSE连接
        connectEventSource();
    }

    main();

})();