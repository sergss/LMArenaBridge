// ==UserScript==
// @name         Мост API LMArena
// @namespace    http://tampermonkey.net/
// @version      2.5
// @description  Соединяет LMArena с локальным API-сервером через WebSocket для упрощённой автоматизации.
// @author       Lianues
// @match        https://lmarena.ai/*
// @match        https://*.lmarena.ai/*
// @icon         https://www.google.com/s2/favicons?sz=64&domain=lmarena.ai
// @grant        none
// @run-at       document-end
// ==/UserScript==

(function () {
    'use strict';

    // --- Конфигурация ---
    const SERVER_URL = "ws://localhost:5102/ws"; // Соответствует порту в api_server.py
    let socket;
    let isCaptureModeActive = false; // Флаг режима захвата идентификаторов

    // --- Основная логика ---
    function connect() {
        console.log(`[Мост API] Устанавливается соединение с локальным сервером: ${SERVER_URL}...`);
        socket = new WebSocket(SERVER_URL);

        socket.onopen = () => {
            console.log("[Мост API] ✅ WebSocket-соединение с локальным сервером установлено.");
            document.title = "✅ " + document.title;
        };

        socket.onmessage = async (event) => {
            try {
                const message = JSON.parse(event.data);

                // Проверка, является ли сообщение командой, а не стандартным запросом чата
                if (message.command) {
                    console.log(`[Мост API] ⬇️ Получена команда: ${message.command}`);
                    if (message.command === 'refresh' || message.command === 'reconnect') {
                        console.log(`[Мост API] Получена команда '${message.command}', выполняется обновление страницы...`);
                        location.reload();
                    } else if (message.command === 'activate_id_capture') {
                        console.log("[Мост API] ✅ Режим захвата идентификаторов активирован. Пожалуйста, выполните операцию 'Retry' на странице.");
                        isCaptureModeActive = true;
                        // Визуальная подсказка для пользователя
                        document.title = "🎯 " + document.title;
                    } else if (message.command === 'send_page_source') {
                        console.log("[Мост API] Получена команда на отправку исходного кода страницы, выполняется отправка...");
                        sendPageSource();
                    }
                    return;
                }

                const { request_id, payload } = message;

                if (!request_id || !payload) {
                    console.error("[Мост API] Получено недействительное сообщение от сервера:", message);
                    return;
                }
                
                console.log(`[Мост API] ⬇️ Получен запрос чата ${request_id.substring(0, 8)}. Подготовка к выполнению fetch-запроса.`);
                await executeFetchAndStreamBack(request_id, payload);

            } catch (error) {
                console.error("[Мост API] Ошибка при обработке сообщения от сервера:", error);
            }
        };

        socket.onclose = () => {
            console.warn("[Мост API] 🔌 Соединение с локальным сервером разорвано. Повторная попытка подключения через 5 секунд...");
            if (document.title.startsWith("✅ ")) {
                document.title = document.title.substring(2);
            }
            setTimeout(connect, 5000);
        };

        socket.onerror = (error) => {
            console.error("[Мост API] ❌ Ошибка WebSocket:", error);
            socket.close(); // Запускает логику переподключения через onclose
        };
    }

    async function executeFetchAndStreamBack(requestId, payload) {
        console.log(`[Мост API] Текущий домен: ${window.location.hostname}`);
        const { is_image_request, message_templates, target_model_id, session_id, message_id } = payload;

        // --- Использование информации о сессии, переданной от сервера ---
        if (!session_id || !message_id) {
            const errorMsg = "Информация о сессии (session_id или message_id) от сервера пуста. Пожалуйста, сначала запустите скрипт `id_updater.py` для настройки.";
            console.error(`[Мост API] ${errorMsg}`);
            sendToServer(requestId, { error: errorMsg });
            sendToServer(requestId, "[DONE]");
            return;
        }

        // URL одинаков для чата и генерации изображений
        const apiUrl = `/nextjs-api/stream/retry-evaluation-session-message/${session_id}/messages/${message_id}`;
        const httpMethod = 'PUT';
        
        console.log(`[Мост API] Используется API-эндпоинт: ${apiUrl}`);
        
        const newMessages = [];
        let lastMsgIdInChain = null;

        if (!message_templates || message_templates.length === 0) {
            const errorMsg = "Список сообщений от сервера пуст.";
            console.error(`[Мост API] ${errorMsg}`);
            sendToServer(requestId, { error: errorMsg });
            sendToServer(requestId, "[DONE]");
            return;
        }

        // Эта логика цикла универсальна для чата и генерации изображений, так как сервер подготовил правильные message_templates
        for (let i = 0; i < message_templates.length; i++) {
            const template = message_templates[i];
            const currentMsgId = crypto.randomUUID();
            const parentIds = lastMsgIdInChain ? [lastMsgIdInChain] : [];
            
            // Для запросов генерации изображений статус всегда 'success'
            // Иначе только последнее сообщение имеет статус 'pending'
            const status = is_image_request ? 'success' : ((i === message_templates.length - 1) ? 'pending' : 'success');

            newMessages.push({
                role: template.role,
                content: template.content,
                id: currentMsgId,
                evaluationId: null,
                evaluationSessionId: session_id,
                parentMessageIds: parentIds,
                experimental_attachments: Array.isArray(template.attachments) ? template.attachments : [],
                failureReason: null,
                metadata: null,
                participantPosition: template.participantPosition || "a",
                createdAt: new Date().toISOString(),
                updatedAt: new Date().toISOString(),
                status: status,
            });
            lastMsgIdInChain = currentMsgId;
        }

        const body = {
            messages: newMessages,
            modelId: target_model_id,
        };

        console.log("[Мост API] Окончательная нагрузка для отправки в API LMArena:", JSON.stringify(body, null, 2));

        // Устанавливаем флаг, чтобы перехватчик fetch знал, что это запрос от скрипта
        window.isApiBridgeRequest = true;
        try {
            const response = await fetch(apiUrl, {
                method: httpMethod,
                headers: {
                    'Content-Type': 'text/plain;charset=UTF-8', // LMArena использует text/plain
                    'Accept': '*/*',
                },
                body: JSON.stringify(body),
                credentials: 'include' // Необходимо включить cookies
            });

            if (!response.ok || !response.body) {
                const errorBody = await response.text();
                throw new Error(`Сетевой ответ некорректен. Статус: ${response.status}. Содержимое: ${errorBody}`);
            }

            const reader = response.body.getReader();
            const decoder = new TextDecoder();

            while (true) {
                const { value, done } = await reader.read();
                if (done) {
                    console.log(`[Мост API] ✅ Поток для запроса ${requestId.substring(0, 8)} успешно завершён.`);
                    // Отправляем [DONE] только после успешного завершения потока
                    sendToServer(requestId, "[DONE]");
                    break;
                }
                const chunk = decoder.decode(value);
                // Пересылаем необработанные данные обратно на сервер
                sendToServer(requestId, chunk);
            }

        } catch (error) {
            console.error(`[Мост API] ❌ Ошибка при выполнении fetch для запроса ${requestId.substring(0, 8)}:`, error);
            // При ошибке отправляем только сообщение об ошибке, без [DONE]
            sendToServer(requestId, { error: error.message });
        } finally {
            // Сбрасываем флаг после завершения запроса, независимо от результата
            window.isApiBridgeRequest = false;
        }
    }

    function sendToServer(requestId, data) {
        if (socket && socket.readyState === WebSocket.OPEN) {
            const message = {
                request_id: requestId,
                data: data
            };
            socket.send(JSON.stringify(message));
        } else {
            console.error("[Мост API] Не удалось отправить данные, WebSocket-соединение не открыто.");
        }
    }

    // --- Перехват сетевых запросов ---
    const originalFetch = window.fetch;
    window.fetch = function(...args) {
        const urlArg = args[0];
        let urlString = '';

        // Убедимся, что URL всегда обрабатывается как строка
        if (urlArg instanceof Request) {
            urlString = urlArg.url;
        } else if (urlArg instanceof URL) {
            urlString = urlArg.href;
        } else if (typeof urlArg === 'string') {
            urlString = urlArg;
        }

        // Проверяем URL только если он является строкой
        if (urlString) {
            const match = urlString.match(/\/nextjs-api\/stream\/retry-evaluation-session-message\/([a-f0-9-]+)\/messages\/([a-f0-9-]+)/);

            // Обновляем идентификаторы только если запрос не от моста API и режим захвата активен
            if (match && !window.isApiBridgeRequest && isCaptureModeActive) {
                const sessionId = match[1];
                const messageId = match[2];
                console.log(`[Перехватчик Моста API] 🎯 Захвачены идентификаторы в активном режиме! Отправка...`);

                // Отключаем режим захвата, чтобы отправить только один раз
                isCaptureModeActive = false;
                if (document.title.startsWith("🎯 ")) {
                    document.title = document.title.substring(2);
                }

                // Асинхронно отправляем захваченные идентификаторы на локальный скрипт id_updater.py
                fetch('http://127.0.0.1:5103/update', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ sessionId, messageId })
                })
                .then(response => {
                    if (!response.ok) throw new Error(`Сервер ответил статусом: ${response.status}`);
                    console.log(`[Мост API] ✅ Идентификаторы успешно отправлены. Режим захвата автоматически отключён.`);
                })
                .catch(err => {
                    console.error('[Мост API] Ошибка при отправке обновления идентификаторов:', err.message);
                    // Режим захвата отключается даже при ошибке, чтобы избежать повторных попыток
                });
            }
        }

        // Вызываем оригинальную функцию fetch, чтобы не нарушить функциональность страницы
        return originalFetch.apply(this, args);
    };

    // --- Отправка исходного кода страницы ---
    async function sendPageSource() {
        try {
            const htmlContent = document.documentElement.outerHTML;
            await fetch('http://localhost:5102/internal/update_available_models', { // новая конечная точка
                method: 'POST',
                headers: {
                    'Content-Type': 'text/html; charset=utf-8'
                },
                body: htmlContent
            });
            console.log("[Мост API] Исходный код страницы успешно отправлен.");
        } catch (e) {
            console.error("[Мост API] Ошибка при отправке исходного кода страницы:", e);
        }
    }

    // --- Запуск соединения ---
    console.log("========================================");
    console.log("  Мост API LMArena v2.5 запущен.");
    console.log("  - Функциональность чата подключена к ws://localhost:5102");
    console.log("  - Захват идентификаторов отправляется на http://localhost:5103");
    console.log("========================================");
    
    connect(); // Устанавливаем WebSocket-соединение

})();