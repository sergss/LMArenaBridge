// ==UserScript==
// @name         LMArena Automator
// @namespace    http://tampermonkey.net/
// @version      1.6.8
// @description  Server-Side Port Balancing.
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

    // --- Multi-Port Configuration (v1.6.8+) ---
    const BASE_HOST = 'http://127.0.0.1';
    let API_PORT = 5102; // Default API port, will be updated from config
    let API_URL = `${BASE_HOST}:${API_PORT}`; // URL for config and port requests
    let WORKER_URL = ''; // URL for SSE connection, determined by server

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
        // 日志总是发送到 API 端口
        const logUrl = WORKER_URL || API_URL; // 如果 worker URL 可用则用它，否则用 API URL
        try {
            await fetch(`${logUrl}/log_from_client?tab_id=${tabId}`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ level, message, tab_id: tabId })
            });
        } catch (e) {
            // Suppress failed-to-fetch errors for logging to avoid console spam
            // originalConsole.error("[Automator] Failed to send log to server:", e);
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

    async function loadConfigAndGetPort() {
        // 1. 从 API 端口加载配置
        console.log(`[Tab ${tabId.substring(0, 4)}] Attempting to load config from API URL: ${API_URL}`);
        try {
            const configResponse = await fetch(`${API_URL}/get_config?tab_id=${tabId}`);
            if (!configResponse.ok) {
                console.error(`Failed to load config, status: ${configResponse.status}. Retrying in 5s.`);
                return false;
            }
            config = await configResponse.json();
            API_PORT = config.api_port || API_PORT; // 更新 API 端口以防万一
            API_URL = `${BASE_HOST}:${API_PORT}`;
            comprehensiveLoggingEnabled = !!config.enable_comprehensive_logging;
            console.log("Config loaded. Comprehensive logging is", comprehensiveLoggingEnabled ? "ENABLED." : "DISABLED.");
            configLoaded = true;
            // 立即处理缓冲的日志
            if (comprehensiveLoggingEnabled) {
                console.log(`Processing ${logBuffer.length} buffered logs...`);
                logBuffer.forEach(log => sendLogToServer(log.level, log.message));
                logBuffer = [];
            }
    
            // 2. 从 API 端口请求一个可用的 Worker 端口
            console.log(`[Tab ${tabId.substring(0, 4)}] Requesting a worker port from ${API_URL}`);
            const portResponse = await fetch(`${API_URL}/get_worker_port?tab_id=${tabId}`);
            if (portResponse.ok) {
                const data = await portResponse.json();
                if (data.status === 'success' && data.port) {
                    WORKER_URL = `${BASE_HOST}:${data.port}`;
                    console.log(`[Tab ${tabId.substring(0, 4)}] Server assigned worker port ${data.port}. Worker URL: ${WORKER_URL}`);
                    return true; // 成功
                }
            }
            // 如果请求失败或服务器返回错误
            const errorData = await portResponse.json().catch(() => ({}));
            console.error(`Failed to get a worker port: ${errorData.message || `Status ${portResponse.status}`}. Retrying in 5s.`);
            return false;
    
        } catch (e) {
            console.error(`Critical error during setup: ${e}. Retrying in 5s.`);
            configLoaded = true; // 即使失败也要允许日志发送
            return false;
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

                const triggerPrefix = '[这条消息仅起占位，请以外部应用中显示的内容为准：/';
                const hangingTrigger = '[防人机检测挂机任务]';

                // 检查是否是普通触发或挂机触发
                if (lastUserMessage && (lastUserMessage.content.startsWith(triggerPrefix) || lastUserMessage.content.startsWith(hangingTrigger))) {
                    console.log('LMArena Automator: Trigger detected. Performing stateful merge...');
                    if (!WORKER_URL) {
                        console.error("Automator Critical Error: Worker URL not set. Cannot get job. Aborting.");
                        return originalFetch.apply(this, args);
                    }
                    console.log('Automator Debug: Original body:', bodyObject);

                    try {
                        const serverResponse = await fetch(`${WORKER_URL}/get_messages_job?tab_id=${tabId}`);
                        const data = await serverResponse.json();

                        if (data.status === 'success' && data.job) {
                            const { message_templates, target_model_id, task_id } = data.job;
                            sessionStorage.setItem('current_task_id', task_id);

                            const isHangingJob = task_id.startsWith('hanging-');

                            // 关键修复：无论是否为挂机任务，都先从原始消息中获取会话ID
                            const originalLastMsg = bodyObject.messages[bodyObject.messages.length - 1];
                            const { evaluationId, evaluationSessionId } = originalLastMsg || {};

                            if (!evaluationSessionId) {
                                console.error("Automator Critical Error: Could not extract evaluationSessionId. Aborting merge.");
                                return originalFetch.apply(this, args); // 中断合并，执行原始请求
                            }

                            // 对于所有任务，我们都将完全替换历史记录，而不是追加。
                            const baseMessages = [];

                            let parentMessageId = null;
                            const newMessages = message_templates.map(template => {
                                const newMsg = {
                                    ...template, id: crypto.randomUUID(),
                                    evaluationId,
                                    evaluationSessionId, // 确保此ID被正确传递
                                    parentMessageIds: parentMessageId ? [parentMessageId] : [],
                                    experimental_attachments: [], failureReason: null, metadata: null,
                                    participantPosition: "a", createdAt: new Date().toISOString(),
                                    updatedAt: new Date().toISOString(),
                                    status: template.role === 'assistant' ? 'pending' : 'success',
                                };
                                parentMessageId = newMsg.id;
                                return newMsg;
                            });

                            bodyObject.messages = baseMessages.concat(newMessages);
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
                        } else {
                             console.warn("Automator: Trigger detected but no job received from server. Proceeding with original request.");
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
            let status = 'completed'; // 默认任务状态为成功

            try {
                while (true) {
                    const { done, value } = await reader.read();
                    if (done) {
                        break; // 流正常结束
                    }
                    const chunk = decoder.decode(value, { stream: true });

                    // 尝试解析服务器可能发送的错误信息
                    if (chunk.includes('"type": "automator_error"')) {
                        console.error("[Automator] Server reported a stream error:", chunk);
                        status = 'failed';
                        break; // 检测到错误，中断循环
                    }

                    // 转发数据块到服务器
                    if (!WORKER_URL) continue; // 如果 worker URL 无效则跳过
                    await fetch(`${WORKER_URL}/stream_chunk`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ task_id: taskId, tab_id: tabId, chunk: chunk }) });
                }
            } catch (e) {
                console.error("[Automator] Error while reading response stream:", e);
                status = 'failed'; // 读取流时发生网络等错误
            } finally {
                if (!WORKER_URL) {
                     console.error("[Automator] Cannot report result: Worker URL is not set.");
                } else {
                    // 无论成功或失败，都向服务器报告结果并清理会话
                    console.log(`[Automator] Task ${taskId.substring(0, 4)} finished with status: ${status}. Preparing to report result and clean up.`);
                    try {
                        await fetch(`${WORKER_URL}/report_result`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ task_id: taskId, tab_id: tabId, status: status }) });
                        console.log(`[Automator] Successfully reported result for task ${taskId.substring(0, 4)}.`);
                    } catch (e) {
                        console.error(`[Automator] Failed to report result for task ${taskId.substring(0, 4)}. Will proceed with cleanup anyway. Error:`, e);
                    }
                }

                console.log(`[Automator] Cleaning up session for task ${taskId.substring(0, 4)}.`);
                sessionStorage.removeItem('current_task_id');
                console.log(`[Automator] Session cleaned. Tab is now idle and ready for new jobs.`);
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
        if (!WORKER_URL) {
            console.error("Cannot connect to SSE: Worker URL not set. Retrying setup...");
            setTimeout(main, 5000); // 重新执行主设置流程
            return;
        }
    
        console.log(`[Tab ${tabId.substring(0, 4)}] Connecting to SSE endpoint at ${WORKER_URL}...`);
        const isHanging = sessionStorage.getItem('lmarena_is_hanging') === 'true';
        console.log(`[Tab ${tabId.substring(0, 4)}] Reporting hanging status to server: ${isHanging}`);
        const eventSource = new EventSource(`${WORKER_URL}/events?tab_id=${tabId}&is_hanging=${isHanging}`);
    
        eventSource.onopen = () => {
            console.log(`[Tab ${tabId.substring(0, 4)}] SSE connection to ${WORKER_URL} established.`);
        };

        eventSource.addEventListener('new_job', async (event) => {
            console.log(`[Tab ${tabId.substring(0, 4)}] New job received via SSE:`, event.data);
            const job = JSON.parse(event.data);

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

        eventSource.addEventListener('set_hanging_status', (event) => {
            console.log(`[Tab ${tabId.substring(0, 4)}] Received hanging status update:`, event.data);
            const data = JSON.parse(event.data);
            sessionStorage.setItem('lmarena_is_hanging', data.is_hanging); // 保存状态
            const hangingPrefix = "【挂机】";

            // 移除可能已存在的前缀，以避免重复添加
            if (document.title.startsWith(hangingPrefix)) {
                document.title = document.title.substring(hangingPrefix.length);
            }

            if (data.is_hanging) {
                document.title = hangingPrefix + document.title;
            }
        });

        // 新增：处理服务器发送的刷新请求
        eventSource.addEventListener('refresh', () => {
            console.warn(`[Tab ${tabId.substring(0, 4)}] 收到服务器的刷新请求（任务超时）。正在刷新页面...`);
            // 设置标志，表明即将进行第一次自动刷新
            sessionStorage.setItem('auto_refresh_pending', 'true');
            // 延迟 1 秒刷新，以确保日志有机会发送到服务器
            setTimeout(() => {
                location.reload();
            }, 1000);
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
        // 页面源数据发送到 API 端口
        if (document.readyState === "complete") {
            console.log(`[Tab ${tabId.substring(0, 4)}] Page loaded. Sending source to API server for model check...`);
            try {
                const htmlContent = document.documentElement.outerHTML;
                await fetch(`${API_URL}/update_models?tab_id=${tabId}`, {
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
        // 检查是否需要进行第二次刷新
        if (sessionStorage.getItem('auto_refresh_pending') === 'true') {
            console.warn(`[Tab ${tabId.substring(0, 4)}] 检测到第一次自动刷新完成。正在执行第二次刷新以确保状态清除...`);
            sessionStorage.removeItem('auto_refresh_pending'); // 清除标志
            location.reload();
            return; // 停止执行脚本的其余部分，等待第二次刷新完成
        }
    
        sessionStorage.removeItem('current_task_id');
        hookFetch();
    
        // 循环直到成功获取配置和 Worker 端口
        while (true) {
            const success = await loadConfigAndGetPort();
            if (success) {
                break; // 成功，跳出循环
            }
            // 失败则等待5秒后重试
            await new Promise(resolve => setTimeout(resolve, 5000));
        }
    
        // 先处理一次性的页面加载任务
        if (document.readyState === "complete") {
            await sendPageSource();
        } else {
            window.addEventListener('load', sendPageSource);
        }
    
        // 在所有一次性任务完成后，再建立持久的SSE连接
        connectEventSource();
    }

    main();

})();