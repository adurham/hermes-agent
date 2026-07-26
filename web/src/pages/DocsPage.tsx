import { useLayoutEffect } from "react";
import { ExternalLink } from "lucide-react";
import { useI18n } from "@/i18n";
import { usePageHeader } from "@/contexts/usePageHeader";
import { cn } from "@/lib/utils";
import { PluginSlot } from "@/plugins";

export const HERMES_DOCS_URL = "https://github.com/adurham/hermes-agent/tree/main/website/docs";

const DS_BUTTON_OUTLINED_LINK_CN = cn(
  "group relative inline-grid grid-cols-[auto_1fr_auto] items-center",
  "px-[.9em_.75em] py-[1.25em] gap-2",
  "leading-0 font-bold tracking-[0.2em] uppercase",
  "text-midground bg-transparent shadow-midground",
  "shadow-[inset_-1px_-1px_0_0_#00000080,inset_1px_1px_0_0_#ffffff80]",
);

export default function DocsPage() {
  const { t } = useI18n();
  const { setEnd } = usePageHeader();

  useLayoutEffect(() => {
    setEnd(
      <a
        href={HERMES_DOCS_URL}
        target="_blank"
        rel="noopener noreferrer"
        className={DS_BUTTON_OUTLINED_LINK_CN}
      >
        <ExternalLink className="size-3.5" />
        {t.app.openDocumentation}
      </a>,
    );
    return () => {
      setEnd(null);
    };
  }, [setEnd, t]);

  return (
    <div
      className={cn(
        "flex min-h-0 w-full min-w-0 flex-1 flex-col",
        "pt-1 sm:pt-2",
      )}
    >
      <PluginSlot name="docs:top" />
      {/* GitHub sends X-Frame-Options/CSP frame-ancestors on tree/blob
          views, so HERMES_DOCS_URL can never render in an iframe (unlike
          the previously-iframed Docusaurus site). Point at the external
          link instead of shipping a permanently blank pane. */}
      <div
        className={cn(
          "flex min-h-0 w-full min-w-0 flex-1 flex-col items-center justify-center gap-4",
          "rounded-sm border border-current/20",
        )}
      >
        <a
          href={HERMES_DOCS_URL}
          target="_blank"
          rel="noopener noreferrer"
          className={DS_BUTTON_OUTLINED_LINK_CN}
        >
          <ExternalLink className="size-3.5" />
          {t.app.openDocumentation}
        </a>
      </div>
      <PluginSlot name="docs:bottom" />
    </div>
  );
}
