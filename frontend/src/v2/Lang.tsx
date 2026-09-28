import { useTranslation } from "react-i18next";

/** The V2 language toggle (top bar of the console and of the sign-in pages). */
export function V2Lang() {
  const { i18n } = useTranslation();
  const lang = i18n.resolvedLanguage ?? "en";
  return (
    <div className="v2-lang">
      <button type="button" className={lang.startsWith("zh") ? "on" : ""} onClick={() => void i18n.changeLanguage("zh-CN")}>
        中文
      </button>
      <button type="button" className={lang === "en" ? "on" : ""} onClick={() => void i18n.changeLanguage("en")}>
        EN
      </button>
    </div>
  );
}
