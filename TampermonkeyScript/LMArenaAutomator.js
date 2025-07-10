// ==UserScript==
// @name         LMArena Automator
// @namespace    http://tampermonkey.net/
// @version      2.0
// @description  Injects history, automates prompts, and supports Tavern Mode for LMArena.
// @author       Lianues
// @match        https://lmarena.ai/c/*
// @grant        none
// @run-at       document-start
// ==/UserScript==

(function() {
    'use strict';

    let config = {
        bypass_enabled: false,
        tavern_mode_enabled: false,
        log_server_requests: false,
        log_tampermonkey_debug: false
    };

    // --- 配置加载逻辑 (v2 - 从服务器获取) ---
    async function loadConfig() {
        try {
            const response = await fetch(`${SERVER_URL}/get_config`);
            if (response.ok) {
                config = await response.json();
                console.log("LMArena Automator: Config loaded successfully from server.", config);
            } else {
                 console.warn(`LMArena Automator: Failed to fetch config from server (status: ${response.status}). Using default values.`);
            }
        } catch (e) {
            console.error("LMArena Automator: Error fetching config from server. Using default values.", e);
        }
    }


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


        // Final, more robust string manipulation with extensive logging
        function modifyPayload(payload) {
            if (typeof payload !== 'string' || !payload.includes('"EvaluationStoreProvider"') || !payload.includes('"initialState"')) {
                return payload;
            }

            if (config.log_tampermonkey_debug) {
                console.log("LMArena Automator DEBUG: Entering modifyPayload.");
                console.log("LMArena Automator DEBUG: history_data object:", history_data);
            }

            // Define the boundaries more robustly
            const startMarker = '"initialState"';
            const endMarker = ',"data-sentry-element":"EvaluationStoreProvider"';

            // --- CORE FIX: Reverse Search ---
            // First, find the unique end marker for our target component.
            const endIndex = payload.indexOf(endMarker);
            if (endIndex === -1) {
                console.log("LMArena History Forger DEBUG: endMarker not found. Cannot proceed.");
                return payload;
            }

            // Now, search backwards from the end marker to find the *closest* start marker.
            // This ensures we get the initialState for the correct component.
            const startIndex = payload.lastIndexOf(startMarker, endIndex);
            if (startIndex === -1) {
                console.log("LMArena History Forger DEBUG: startMarker not found before endMarker.");
                return payload;
            }
            // --- END OF CORE FIX ---

            if (config.log_tampermonkey_debug) {
                const originalBlock = payload.substring(startIndex, endIndex);
                console.log("LMArena Automator DEBUG: Original block to be replaced:", originalBlock);
            }

            // Extract the parts of the string we want to keep
            const beforePart = payload.substring(0, startIndex);
            const afterPart = payload.substring(endIndex); // This includes the endMarker itself

            // Construct the new, correctly formatted initialState property
            const newInitialState = `"initialState":${JSON.stringify(history_data)}`;

            if (config.log_tampermonkey_debug) {
                console.log("LMArena Automator DEBUG: New initialState block:", newInitialState);
            }

            // Assemble the final payload
            const newPayload = beforePart + newInitialState + afterPart;


            if (payload !== newPayload) {
                console.log('LMArena Automator: Successfully injected conversation history (using robust boundary replacement).');
                if (config.log_tampermonkey_debug) {
                    console.log("LMArena Automator DEBUG: Final payload (first 500 chars):", newPayload.substring(0, 500));
                }
                return newPayload;
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
                
                try {
                    const originalOptions = args[1] || {};
                    let bodyObject = JSON.parse(originalOptions.body);

                    // 【【【新功能：酒馆模式请求体修改】】】
                    if (config.tavern_mode_enabled) {
                        const messages = bodyObject.messages;
                        // 检查是否是我们的触发请求：最后一条用户消息内容是单个空格，且后面紧跟一个助手占位符
                        if (messages && messages.length > 1) {
                            const lastUserMessageIndex = messages.length - 2;
                            const lastUserMessage = messages[lastUserMessageIndex];
                            const assistantPlaceholder = messages[messages.length - 1];

                            // 精确匹配触发文本
                            if (lastUserMessage.role === 'user' && lastUserMessage.content === '[TAVERN_MODE_TRIGGER]' && assistantPlaceholder.role === 'assistant') {
                                console.log("LMArena Automator [Tavern Mode]: Detected trigger request. Modifying body...");

                                // 移除我们添加的空格用户消息
                                messages.splice(lastUserMessageIndex, 1);

                                // 获取新的倒数第二条消息（现在是原始对话的最后一条）
                                const newLastMessage = messages.length > 1 ? messages[messages.length - 2] : null;

                                if (newLastMessage) {
                                    // 更新助手占位符的父ID，使其指向它前面的那条消息
                                    assistantPlaceholder.parentMessageIds = [newLastMessage.id];
                                    console.log(`LMArena Automator [Tavern Mode]: Assistant placeholder's parent ID updated to ${newLastMessage.id}.`);
                                } else if (messages.length === 1 && messages[0].role === 'assistant') {
                                    // 如果移除后只剩下助手占位符，说明这是第一条消息
                                    assistantPlaceholder.parentMessageIds = [];
                                    console.log("LMArena Automator [Tavern Mode]: This is the first message, parent ID cleared.");
                                }
                                
                                console.log("LMArena Automator [Tavern Mode]: Body successfully modified.");
                            }
                        }
                    }

                    // 首先处理模型ID替换 (原有逻辑)
                    const targetModelId = localStorage.getItem(MODEL_ID_STORAGE_KEY);
                    if (targetModelId) {
                        bodyObject.modelAId = targetModelId;
                        if (bodyObject.messages && bodyObject.messages.length > 0) {
                            const lastMessage = bodyObject.messages[bodyObject.messages.length - 1];
                            if (lastMessage.role === 'assistant') {
                                lastMessage.modelId = targetModelId;
                            }
                        }
                        console.log('LMArena Automator: Modified request body with new model ID.');
                    }

                    // 【【【新功能：注入空 User 请求 (v3 - 正确实现)】】】
                    if (config.bypass_enabled && bodyObject.messages && bodyObject.messages.length >= 2) {
                        console.log("LMArena Automator: Bypass enabled. Modifying request body to inject empty message.");
                        
                        const messages = bodyObject.messages;
                        const originalUserMessage = messages[messages.length - 2];
                        const assistantPlaceholder = messages[messages.length - 1];

                        if (originalUserMessage.role === 'user' && assistantPlaceholder.role === 'assistant') {
                            const emptyUserMessage = {
                                ...originalUserMessage, // 继承大部分属性
                                id: crypto.randomUUID(), // 生成新的唯一ID
                                content: " ", // 内容为空
                                parentMessageIds: [originalUserMessage.id] // 父消息是原始的用户消息
                            };

                            // 更新模型占位符的父ID，使其指向新的空消息
                            assistantPlaceholder.parentMessageIds = [emptyUserMessage.id];
                            
                            // 在原始用户消息和模型占位符之间插入新消息
                            messages.splice(messages.length - 1, 0, emptyUserMessage);
                            
                            console.log("LMArena Automator: Successfully injected empty user message into the request.");
                        } else {
                            console.warn("LMArena Automator: Bypass logic failed. Message structure did not match expected 'user' -> 'assistant' at the end.");
                        }
                    }
                    
                    // 使用修改后的 body 更新请求参数
                    newArgs[1] = { ...originalOptions, body: JSON.stringify(bodyObject) };

                } catch (e) {
                    console.error("LMArena Automator: Error processing fetch request:", e);
                    // 如果处理失败，继续使用原始参数
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


        // Helper function to wait for an element with React props
        async function findElementWithReactProps(selector, timeout = 30000) {
            const startTime = Date.now();
            while (Date.now() - startTime < timeout) {
                const element = document.querySelector(selector);
                if (element) {
                    const reactPropsKey = Object.keys(element).find(key => key.startsWith('__reactProps$'));
                    if (reactPropsKey) {
                        return element; // Found element with React props
                    }
                }
                // Wait a bit before retrying
                await new Promise(resolve => setTimeout(resolve, 200));
            }
            console.error(`LMArena Automator: Timed out waiting for element with React props: ${selector}`);
            return null; // Timed out
        }
    
        // --- 新功能：发送 Prompt (v3 - React Internals, with polling) ---
        async function typeAndSubmitPrompt(promptText) {
            console.log(`LMArena Automator: Typing prompt via React Internals: "${promptText}"`);
    
            const textarea = await findElementWithReactProps('textarea[name="text"]');
            if (!textarea) {
                // Error is already logged by the helper function
                return;
            }
    
            const submitButton = document.querySelector('button[type="submit"]');
            if (!submitButton) {
                console.error("LMArena Automator: Submit button not found.");
                return;
            }
    
            // 找到 React Fiber 节点的 key (guaranteed by helper)
            const reactPropsKey = Object.keys(textarea).find(key => key.startsWith('__reactProps$'));
    
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
            await new Promise(resolve => setTimeout(resolve, 150));
    
            if (!submitButton.disabled) {
                submitButton.click();
                console.log("LMArena Automator: Prompt submitted successfully.");
            } else {
                // Sometimes React takes a moment longer to enable the button. Retry once.
                await new Promise(resolve => setTimeout(resolve, 200));
                if (!submitButton.disabled) {
                    submitButton.click();
                    console.log("LMArena Automator: Prompt submitted successfully (on second try).");
                } else {
                    console.error("LMArena Automator: Submit button is still disabled after typing.");
                }
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
        await loadConfig(); // 加载配置
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

                // 【【【最终修复：等待 DOM 就绪后再发送完成信号】】】
                if (injectionId) {
                    console.log(`LMArena History Forger: Waiting for DOM to be ready before sending signal for ${injectionId}...`);
                    
                    const startTime = Date.now();
                    const interval = setInterval(async () => {
                        const textarea = document.querySelector('textarea[name="text"]');
                        const submitButton = document.querySelector('button[type="submit"]');

                        // 检查元素是否存在
                        if (textarea && submitButton) {
                            clearInterval(interval);
                            console.log(`LMArena History Forger: DOM is ready. Sending completion signal for ${injectionId}.`);
                            try {
                                await fetch(`${SERVER_URL}/signal_injection_complete`, {
                                    method: 'POST',
                                    headers: {'Content-Type': 'application/json'},
                                    body: JSON.stringify({
                                        injection_id: injectionId,
                                        page_html: document.documentElement.outerHTML
                                    })
                                });
                                console.log("LMArena History Forger: Completion signal sent successfully.");
                            } catch (e) {
                                console.error("LMArena History Forger: Failed to send completion signal:", e);
                            }
                        } else if (Date.now() - startTime > 15000) { // 15秒超时
                            clearInterval(interval);
                            console.error("LMArena History Forger: Timed out waiting for DOM to become ready. Sending signal anyway to avoid blocking server.");
                             await fetch(`${SERVER_URL}/signal_injection_complete`, {
                                method: 'POST',
                                headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify({
                                    injection_id: injectionId,
                                    error: "timeout",
                                    page_html: document.documentElement.outerHTML
                                })
                            });
                        }
                    }, 200); // 每 200ms 检查一次
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