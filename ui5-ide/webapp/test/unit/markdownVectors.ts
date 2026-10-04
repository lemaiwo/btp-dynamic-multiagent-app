/** Markup that would make a browser fetch a remote resource, shared by the markdown and docView tests. */

export const R = "https://tracker.example.com";

/** [label, raw HTML, tags that must not survive] */
export const FETCH_VECTORS: [string, string, string[]][] = [
    ["img srcset", `<img srcset="${R}/a.png 1x, ${R}/b.png 2x" alt="x">`, []],
    ["img remote src and srcset", `<img src="${R}/a.png" srcset="${R}/a.png 1x">`, []],
    ["video poster and src", `<video poster="${R}/p.png" src="${R}/v.mp4" autoplay></video>`, ["video"]],
    ["audio autoplay src", `<audio autoplay src="${R}/a.mp3"></audio>`, ["audio"]],
    ["table background", `<table background="${R}/b.png"><tr><td background="${R}/c.png">x</td></tr></table>`, []],
    ["picture source srcset", `<picture><source srcset="${R}/a.png"><img src="data:image/png;base64,iVBORw0KGgo="></picture>`,
        ["picture", "source"]],
    ["video track src", `<video><track src="${R}/t.vtt" default></video>`, ["video", "track"]],
    ["object data", `<object data="${R}/o.swf"></object>`, ["object"]],
    ["embed src", `<embed src="${R}/e.swf">`, ["embed"]],
    ["iframe src", `<iframe src="${R}/f.html"></iframe>`, ["iframe"]],
    ["link stylesheet", `<link rel="stylesheet" href="${R}/s.css">`, ["link"]],
    ["style @import", `<style>@import url(${R}/s.css);</style>`, ["style"]],
    ["base href", `<base href="${R}/">`, ["base"]],
    ["meta refresh", `<meta http-equiv="refresh" content="0;url=${R}/">`, ["meta"]],
    ["svg image href", `<svg><image href="${R}/i.png"></image></svg>`, ["svg", "image"]],
    ["svg image xlink:href", `<svg><image xlink:href="${R}/i.png"></image></svg>`, ["svg", "image"]],
    ["svg use", `<svg><use href="${R}/u.svg#a"></use></svg>`, ["svg", "use"]],
    ["svg feImage", `<svg><filter><feImage href="${R}/f.png"></feImage></filter></svg>`, ["svg", "feimage"]],
    ["svg fill url()", `<svg><rect fill="url(${R}/p.svg#g)"></rect></svg>`, ["svg", "rect"]],
    ["a ping", `<a href="https://example.com" ping="${R}/ping">x</a>`, []],
    ["input image", `<input type="image" src="${R}/i.png">`, ["input"]],
    ["div style url()", `<div style="background:url(${R}/b.png)">x</div>`, []],
    ["blockquote cite", `<blockquote cite="${R}/c">x</blockquote>`, []],
    ["source on audio", `<audio><source src="${R}/a.mp3"></audio>`, ["audio", "source"]],
    ["frameset", `<frameset><frame src="${R}/f.html"></frameset>`, ["frame", "frameset"]],
    ["math href", `<math><mi href="${R}/m">x</mi></math>`, ["math"]]
];

/** Every vector at once plus ordinary markdown. */
export const KITCHEN_SINK = [
    "# Title", "", "Text with [a link](https://example.com/x) and ![img](https://tracker.example.com/i.png).", "",
    "- item", "", "```abap", "DATA x TYPE i.", "```", "", "| a | b |", "|---|---|", "| 1 | 2 |", "",
    ...FETCH_VECTORS.map(([, raw]) => raw), "",
    "<p data-para=\"3\" tabindex=\"1\" title=\"t\">p</p>", "",
    "<img src=\"data:image/png;base64,iVBORw0KGgo=\" alt=\"inline\">"
].join("\n");
