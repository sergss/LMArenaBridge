// ==UserScript==
// @name         LMArena Model Provider Injector (仙之人兮列如麻) V5
// @namespace    http://tampermonkey.net/
// @version      0.6
// @description  NFU discord_id:skuhrasr0plus1向您提供 | Injects provider/org for models missing them in Next.js stream.
// @author       skuhrasr0plus1 & Google AI
// @match        https://lmarena.ai/*
// @match        https://*.lmarena.ai/*
// @grant        none
// @run-at       document-start
// ==/UserScript==

(function() {
    'use strict';

    console.log("Injector V5 Universal: Script started...");

    const PROVIDER_TO_ADD = 'google';
    const ORGANIZATION_TO_ADD = 'google';

    // Generalized Regex:
    // "publicName": - Literal match
    // "([^"]+)"     - Capture the model name (Group 1)
    // ,             - Literal comma
    // (?="capabilities":) - Lookahead to ensure provider/org are missing
    // g             - Global flag (match all occurrences)
    const searchPattern = new RegExp(/"publicName":"([^"]+)",(?="capabilities":)/g);

    // Replacement: Uses $1 to insert the captured model name
    const replacement = `"publicName":"$1","organization":"${ORGANIZATION_TO_ADD}","provider":"${PROVIDER_TO_ADD}",`;

    function modifyPayload(payload) {
        if (typeof payload !== 'string') return payload;

        // Use the generalized regex replacement
        const modifiedPayload = payload.replace(searchPattern, replacement);

        if (modifiedPayload !== payload) {
            console.log('Injector V5 Universal: Successfully added providers to anonymous models.');
            return modifiedPayload;
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

    // --- Timing Handling Logic ---

    if (self.__next_f && typeof self.__next_f.push === 'function') {
        console.log("Injector V5: __next_f already exists. Processing existing data.");

        if (Array.isArray(self.__next_f)) {
             self.__next_f.forEach(processChunk);
        }
        hookPush(self.__next_f);
    }

    let actual_next_f = self.__next_f;

    try {
        const descriptor = Object.getOwnPropertyDescriptor(self, '__next_f');
        if (descriptor && !descriptor.configurable) {
            console.warn("Injector V5: self.__next_f exists and is non-configurable.");
        } else {
             Object.defineProperty(self, '__next_f', {
                set: function(value) {
                    console.log("Injector V5: self.__next_f is being set.");
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
        console.error("Injector V5: Failed to defineProperty on self.__next_f", e);
    }

})();