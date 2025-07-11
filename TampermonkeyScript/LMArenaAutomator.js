// ==UserScript==
// @name         LMArena Automator
// @namespace    http://tampermonkey.net/
// @version      7.1
// @description  Injects history with robust, buffered comprehensive logging and error reporting.
// @author       Lianues
// @match        https://lmarena.ai/c/*
// @grant        none
// @run-at       document-start
// ==/UserScript==

(function() {
    'use strict';

    const SERVER_URL = 'http://127.0.0.1:5102';
    let config = {};
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
            await fetch(`${SERVER_URL}/log_from_client`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ level, message })
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
        console.log("Attempting to load config from server...");
        try {
            const response = await fetch(`${SERVER_URL}/get_config`);
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
                        const serverResponse = await fetch(`${SERVER_URL}/get_messages_job`);
                        const data = await serverResponse.json();

                        if (data.status === 'success' && data.job) {
                            const { message_templates, target_model_id } = data.job;
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
                        await fetch(`${SERVER_URL}/report_result`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ task_id: taskId, status: 'completed' }) });
                        sessionStorage.removeItem('current_task_id');
                        break;
                    }
                    const chunk = decoder.decode(value, {stream: true});
                    await fetch(`${SERVER_URL}/stream_chunk`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ task_id: taskId, chunk: chunk }) });
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

    async function pollForPromptJob() {
        try {
            const response = await fetch(`${SERVER_URL}/get_prompt_job`);
            const data = await response.json();
            if (data.status === 'success' && data.job) {
                sessionStorage.setItem('current_task_id', data.job.task_id);
                await typeAndSubmitPrompt(data.job.prompt);
            }
        } catch (e) {
            // 这个轮询预计会经常失败。日志将无条件调用，但只在总开关打开时发送。
            console.log("Polling for prompt job failed (this is often normal).");
        }
        setTimeout(pollForPromptJob, 2000);
    }

    async function main() {
        await loadConfig();
        hookFetch();
        pollForPromptJob();
    }

    main();

})();