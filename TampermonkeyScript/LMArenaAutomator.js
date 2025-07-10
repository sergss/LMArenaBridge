// ==UserScript==
// @name         LMArena History Forger
// @namespace    http://tampermonkey.net/
// @version      1.0
// @description  Injects custom conversation history into LMArena.
// @author       You
// @match        https://lmarena.ai/c/*
// @grant        none
// @run-at       document-start
// ==/UserScript==

(function() {
    'use strict';

    const SERVER_URL = 'http://127.0.0.1:5102';
    const STORAGE_KEY = 'pending_history_injection';
    const MODEL_ID_STORAGE_KEY = 'lmarena_target_model_id'; // 新增：用于存储模型 ID 的键

    console.log("LMArena History Forger: Script started...");

    // --- 轮询逻辑 ---
    async function pollForJob() {
        console.log("LMArena History Forger: Polling for new injection job...");
        try {
            const response = await fetch(`${SERVER_URL}/get_injection_job`);
            if (!response.ok) {
                throw new Error(`Server responded with status: ${response.status}`);
            }
            const data = await response.json();

            if (data.status === 'success' && data.job) {
                console.log("LMArena History Forger: Received new job. Storing and reloading.", data.job);
                localStorage.setItem(STORAGE_KEY, JSON.stringify(data.job));
                location.reload();
            } else {
                // If no job, wait and poll again
                setTimeout(pollForJob, 1000); // 3-second polling interval
            }
        } catch (error) {
            console.error("LMArena History Forger: Error polling for job:", error);
            // Wait longer before retrying on error
            setTimeout(pollForJob, 10000);
        }
    }

    // --- 注入逻辑 ---
    function performInjection(history_data) {
        // 【【【新功能：从 URL 动态获取会话 ID】】】
        const urlPath = window.location.pathname;
        const match = urlPath.match(/\/c\/([a-f0-9\-]+)/);
        if (!match || !match[1]) {
            console.error("LMArena History Forger: Could not extract session ID from URL:", urlPath);
            // 如果无法获取 ID，为避免注入错误数据，直接返回
            return;
        }
        const currentSessionId = match[1];
        console.log(`LMArena History Forger: Extracted current session ID: ${currentSessionId}`);

        // 【【【核心修复：用当前页面的会话 ID 更新注入数据】】】
        history_data.id = currentSessionId;
        if (history_data.messages && Array.isArray(history_data.messages)) {
            history_data.messages.forEach(msg => {
                msg.evaluationSessionId = currentSessionId;
            });
        }
        console.log("LMArena History Forger: Updated history data with current session ID.");


        // Regex to find the specific initialState object for EvaluationStoreProvider
        const searchPattern = /("initialState"\s*:\s*\{"id":"[a-f0-9\-]+","userId":"[a-f0-9\-]+",.*?"maskedEvaluations":\[.*?\]\s*\},"data-sentry-element":"EvaluationStoreProvider")/g;

        // Replacement string
        const replacement = `"initialState":${JSON.stringify(history_data)},"data-sentry-element":"EvaluationStoreProvider"`;

        function modifyPayload(payload) {
            if (typeof payload !== 'string') return payload;

            if (payload.includes('"EvaluationStoreProvider"')) {
                const modifiedPayload = payload.replace(searchPattern, () => replacement);
                if (modifiedPayload !== payload) {
                    console.log('LMArena History Forger: Successfully injected conversation history.');
                    return modifiedPayload;
                }
            }
            return payload;
        }

        function processChunk(arg) {
            if (Array.isArray(arg)) {
                for (let i = 0; i < arg.length; i++) {
                    if (typeof arg[i] === 'string') {
                        arg[i] = modifyPayload(arg[i]);
                    }
                }
            }
            return arg;
        }

        function hookPush(array) {
            const originalPush = array.push;
            array.push = function(...args) {
                args = args.map(processChunk);
                return originalPush.apply(this, args);
            };
            return array.push;
        }

        if (self.__next_f && typeof self.__next_f.push === 'function') {
            console.log("LMArena History Forger: __next_f already exists. Processing existing data.");
            if (Array.isArray(self.__next_f)) {
                 self.__next_f.forEach(processChunk);
            }
            hookPush(self.__next_f);
        }

        let actual_next_f = self.__next_f;

        try {
            const descriptor = Object.getOwnPropertyDescriptor(self, '__next_f');
            if (descriptor && !descriptor.configurable) {
                console.warn("LMArena History Forger: self.__next_f exists and is non-configurable.");
            } else {
                 Object.defineProperty(self, '__next_f', {
                    set: function(value) {
                        console.log("LMArena History Forger: self.__next_f is being set.");
                        if (value && typeof value.push === 'function') {
                            if (Array.isArray(value)) {
                                 value.forEach(processChunk);
                            }
                            hookPush(value);
                        }
                        actual_next_f = value;
                    },
                    get: function() {
                        return actual_next_f;
                    },
                    configurable: true
                });
            }
        } catch (e) {
            console.error("LMArena History Forger: Failed to defineProperty on self.__next_f", e);
        }
    }


    // --- 网络请求拦截逻辑 (v2) ---
    function hookFetch() {
        const originalFetch = window.fetch;
        window.fetch = async function(...args) {
            const url = args[0] instanceof Request ? args[0].url : args[0];
            let newArgs = [...args]; // 可修改的参数副本

            if (typeof url === 'string' && url.includes('/api/stream/post-to-evaluation/')) {
                console.log('LMArena Automator: Intercepted model evaluation request:', url);

                const targetModelId = localStorage.getItem(MODEL_ID_STORAGE_KEY);
                if (targetModelId) {
                    try {
                        const request = new Request(...args);
                        let body = await request.json();
                        body.modelAId = targetModelId;
                        if (body.messages && body.messages.length > 0) {
                            const lastMessage = body.messages[body.messages.length - 1];
                            if (lastMessage.role === 'assistant') {
                                lastMessage.modelId = targetModelId;
                            }
                        }
                        newArgs[1] = { ...newArgs[1], body: JSON.stringify(body) };
                        console.log('LMArena Automator: Modified request body with new model ID.');
                    } catch (e) {
                        console.error("LMArena Automator: Error modifying fetch request:", e);
                    }
                }
                
                // 执行 fetch
                const response = await originalFetch.apply(this, newArgs);
                const taskId = sessionStorage.getItem('current_task_id');

                if (taskId) {
                    console.log(`LMArena Automator: Processing response stream for task ${taskId}`);
                    // 【【【核心修复】】】克隆响应，一个用于页面渲染，一个用于我们读取
                    const responseClone = response.clone();

                    // 异步处理克隆的响应流，不阻塞主流程
                    (async () => {
                        const reader = responseClone.body.getReader();
                        const decoder = new TextDecoder();
                        while (true) {
                            try {
                                const { done, value } = await reader.read();
                                if (done) {
                                    console.log('LMArena Automator: Stream finished.');
                                    await fetch(`${SERVER_URL}/report_result`, {
                                        method: 'POST',
                                        headers: {'Content-Type': 'application/json'},
                                        body: JSON.stringify({ task_id: taskId, status: 'completed' })
                                    });
                                    sessionStorage.removeItem('current_task_id');
                                    break;
                                }
                                const chunk = decoder.decode(value, {stream: true});
                                await fetch(`${SERVER_URL}/stream_chunk`, {
                                    method: 'POST',
                                    headers: {'Content-Type': 'application/json'},
                                    body: JSON.stringify({ task_id: taskId, chunk: chunk })
                                });
                            } catch (streamError) {
                                console.error("LMArena Automator: Error reading stream:", streamError);
                                await fetch(`${SERVER_URL}/report_result`, {
                                     method: 'POST',
                                     headers: {'Content-Type': 'application/json'},
                                     body: JSON.stringify({ task_id: taskId, status: 'failed' })
                                });
                                sessionStorage.removeItem('current_task_id');
                                break;
                            }
                        }
                    })();
                }
                // 立即返回原始响应，让页面正常渲染
                return response;
            }
            
            return originalFetch.apply(this, args);
        };
        console.log("LMArena Automator: Fetch hooked successfully.");
    }


    // --- 新功能：发送 Prompt (v3 - React Internals) ---
    async function typeAndSubmitPrompt(promptText) {
        console.log(`LMArena Automator: Typing prompt via React Internals: "${promptText}"`);
        const textarea = document.querySelector('textarea[name="text"]');
        const submitButton = document.querySelector('button[type="submit"]');

        if (!textarea || !submitButton) {
            console.error("LMArena Automator: Textarea or submit button not found.");
            return;
        }

        // 找到 React Fiber 节点的 key
        const reactPropsKey = Object.keys(textarea).find(key => key.startsWith('__reactProps$'));
        if (!reactPropsKey) {
            console.error("LMArena Automator: Could not find React props on the textarea.");
            return;
        }

        // 直接调用 React 的 onChange 事件处理器
        const props = textarea[reactPropsKey];
        if (props && typeof props.onChange === 'function') {
            const mockEvent = { target: { value: promptText } };
            props.onChange(mockEvent);
            console.log("LMArena Automator: React onChange handler invoked.");
        } else {
            console.error("LMArena Automator: Could not find onChange handler in React props.");
            return;
        }

        // 等待 React 完成状态更新和重新渲染
        await new Promise(resolve => setTimeout(resolve, 100));

        if (!submitButton.disabled) {
            submitButton.click();
            console.log("LMArena Automator: Prompt submitted successfully via React internals.");
        } else {
            console.error("LMArena Automator: Submit button is still disabled even after using React internals.");
        }
    }

    async function pollForPromptJob() {
        try {
            const response = await fetch(`${SERVER_URL}/get_prompt_job`);
            const data = await response.json();
            if (data.status === 'success' && data.job) {
                console.log("LMArena Automator: Received new prompt job.", data.job);
                // 【【【核心修复】】】存储 task_id 以便 fetch hook 可以捕获响应
                sessionStorage.setItem('current_task_id', data.job.task_id);
                await typeAndSubmitPrompt(data.job.prompt);
            }
        } catch (error) {
            // 静默处理错误
        } finally {
            setTimeout(pollForPromptJob, 3000);
        }
    }


    // --- 主执行逻辑 ---
    async function main() {
        hookFetch(); // 在脚本开始时就注入 fetch 钩子

        const pendingData = localStorage.getItem(STORAGE_KEY);
        if (pendingData) {
            console.log("LMArena History Forger: Found pending data in localStorage. Attempting injection.");
            localStorage.removeItem(STORAGE_KEY); // 清除历史数据
            try {
                const jobData = JSON.parse(pendingData);
                const injectionId = jobData.injection_id; // 获取 injection_id

                if (jobData.targetModelId) {
                    localStorage.setItem(MODEL_ID_STORAGE_KEY, jobData.targetModelId);
                    console.log(`LMArena History Forger: Stored targetModelId (${jobData.targetModelId}) to localStorage.`);
                }

                performInjection(jobData);
                console.log("LMArena History Forger: Injection logic executed.");

                // 【【【修复：移除DOM就绪检查，直接发送完成信号以避免阻塞】】】
                if (injectionId) {
                    console.log(`LMArena History Forger: Sending completion signal for ${injectionId} immediately.`);
                    // 为了确保之前的JS代码有机会执行完毕，我们稍微延迟一下再发送信号
                    setTimeout(async () => {
                        try {
                            await fetch(`${SERVER_URL}/signal_injection_complete`, {
                                method: 'POST',
                                headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify({
                                    injection_id: injectionId,
                                    status: 'completed_without_dom_check',
                                    page_html: document.documentElement.outerHTML // 仍然发送HTML以便调试
                                })
                            });
                            console.log("LMArena History Forger: Completion signal sent successfully (without DOM check).");
                        } catch (e) {
                            console.error("LMArena History Forger: Failed to send completion signal:", e);
                        }
                    }, 500); // 延迟500毫秒
                }

                // 注入完成后，继续轮询新任务
                pollForJob();
                pollForPromptJob();

            } catch (e) {
                console.error("LMArena History Forger: Failed to parse pending data.", e);
                // 即使失败也要继续轮询
                pollForJob();
                pollForPromptJob();
            }
        } else {
            // 没有待处理的注入任务，正常启动轮询
            pollForJob();
            pollForPromptJob();
        }
    }

    main();


})();