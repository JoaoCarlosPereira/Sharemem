"use client"

import { useState } from "react"
import { Eye, EyeOff, Download, Upload } from "lucide-react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./ui/card"
import { Input } from "./ui/input"
import { Label } from "./ui/label"
import { Slider } from "./ui/slider"
import { Switch } from "./ui/switch"
import { Button } from "./ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "./ui/select"
import { Textarea } from "./ui/textarea"
import { useRef, useState as useReactState } from "react"
import { useSelector } from "react-redux"
import { RootState } from "@/store/store"
import { APP_NAME } from "@/lib/branding"
import { getApiUrl } from "@/lib/api-url";
import { CLEARED_SECRET, isMaskedSecret, nextSecretInput } from "@/lib/secret-mask";

interface FormViewProps {
  settings: any
  onChange: (settings: any) => void
}

export function FormView({ settings, onChange }: FormViewProps) {
  const [showLlmAdvanced, setShowLlmAdvanced] = useState(false)
  const [showLlmApiKey, setShowLlmApiKey] = useState(false)
  const [showEmbedderApiKey, setShowEmbedderApiKey] = useState(false)
  const [isUploading, setIsUploading] = useReactState(false)
  const [selectedImportarFileName, setSelectedImportarFileName] = useReactState("")
  const fileInputRef = useRef<HTMLInputElement>(null)
    const userId = useSelector((state: RootState) => state.profile.userId)

  const handleOpenMemoryChange = (key: string, value: any) => {
    onChange({
      ...settings,
      openmemory: {
        ...settings.openmemory,
        [key]: value,
      },
    })
  }

  const handleLlmProviderChange = (value: string) => {
    const prev = settings.mem0?.llm?.config || {}
    const config = { ...prev }
    if (value === "openai" || value === "lmstudio") {
      if (!config.openai_base_url) {
        config.openai_base_url = "http://host.docker.internal:8000/v1"
      }
      if (!config.api_key) {
        config.api_key = "llama.cpp"
      }
    }
    onChange({
      ...settings,
      mem0: {
        ...settings.mem0,
        llm: {
          ...settings.mem0.llm,
          provider: value,
          config,
        },
      },
    })
  }

  const handleLlmConfigChange = (key: string, value: any) => {
    onChange({
      ...settings,
      mem0: {
        ...settings.mem0,
        llm: {
          ...settings.mem0.llm,
          config: {
            ...settings.mem0.llm.config,
            [key]: value,
          },
        },
      },
    })
  }

  const handleEmbedderProviderChange = (value: string) => {
    onChange({
      ...settings,
      mem0: {
        ...settings.mem0,
        embedder: {
          ...settings.mem0.embedder,
          provider: value,
        },
      },
    })
  }

  const handleEmbedderConfigChange = (key: string, value: any) => {
    onChange({
      ...settings,
      mem0: {
        ...settings.mem0,
        embedder: {
          ...settings.mem0.embedder,
          config: {
            ...settings.mem0.embedder.config,
            [key]: value,
          },
        },
      },
    })
  }

  const llmProvider = settings.mem0?.llm?.provider?.toLowerCase() || ""
  const embedderProvider = settings.mem0?.embedder?.provider?.toLowerCase() || ""
  const needsLlmApiKey = llmProvider !== "ollama"
  const needsEmbedderApiKey = embedderProvider !== "ollama"
  const isLlmOllama = llmProvider === "ollama"
  const isEmbedderOllama = embedderProvider === "ollama"
  const usesLlmOpenAiCompatibleUrl =
    llmProvider === "openai" || llmProvider === "lmstudio"
  const usesEmbedderOpenAiCompatibleUrl =
    embedderProvider === "openai" || embedderProvider === "lmstudio"

  const LLM_PROVIDERS = {
    "OpenAI": "openai",
    "Anthropic": "anthropic", 
    "Azure OpenAI": "azure_openai",
    "Ollama": "ollama",
    "Together": "together",
    "Groq": "groq",
    "Litellm": "litellm",
    "Mistral AI": "mistralai",
    "Google AI": "google_ai",
    "AWS Bedrock": "aws_bedrock",
    "Gemini": "gemini",
    "DeepSeek": "deepseek",
    "xAI": "xai",
    "LM Studio": "lmstudio",
    "LangChain": "langchain",
  }

  const EMBEDDER_PROVIDERS = {
    "OpenAI": "openai",
    "Azure OpenAI": "azure_openai", 
    "Ollama": "ollama",
    "Hugging Face": "huggingface",
    "Vertex AI": "vertexai",
    "Gemini": "gemini",
    "LM Studio": "lmstudio",
    "Together": "together",
    "LangChain": "langchain",
    "AWS Bedrock": "aws_bedrock",
  }

  return (
    <div className="space-y-8">
      {/* Configurações da plataforma */}
      <Card>
        <CardHeader>
          <CardTitle>Configurações do {APP_NAME}</CardTitle>
          <CardDescription>Configure as opções da sua instância {APP_NAME}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="space-y-2">
            <Label htmlFor="custom-instructions">Instruções personalizadas</Label>
            <Textarea
              id="custom-instructions"
              placeholder="Insira instruções personalizadas para gerenciamento de memórias..."
              value={settings.openmemory?.custom_instructions || ""}
              onChange={(e) => handleOpenMemoryChange("custom_instructions", e.target.value)}
              className="min-h-[100px]"
            />
            <p className="text-xs text-muted-foreground mt-1">
              Instruções usadas para guiar o processamento de memórias e extração de fatos.
            </p>
          </div>
          <div className="flex items-center justify-between rounded-lg border p-4">
            <div className="space-y-1">
              <Label htmlFor="multilingual">Memórias no idioma de entrada</Label>
              <p className="text-xs text-muted-foreground">
                Quando ativado, as memórias são extraídas no mesmo idioma das mensagens (ex.: PT-BR).
              </p>
            </div>
            <Switch
              id="multilingual"
              checked={settings.openmemory?.multilingual ?? true}
              onCheckedChange={(checked) => handleOpenMemoryChange("multilingual", checked)}
            />
          </div>
        </CardContent>
      </Card>

      {/* Configurações de LLM */}
      <Card>
        <CardHeader>
          <CardTitle>Configurações de LLM</CardTitle>
          <CardDescription>Configure o provedor e as opções do LLM</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="space-y-2">
            <Label htmlFor="llm-provider">Provedor de LLM</Label>
            <Select 
              value={settings.mem0?.llm?.provider || ""}
              onValueChange={handleLlmProviderChange}
            >
              <SelectTrigger id="llm-provider">
                <SelectValue placeholder="Selecione um provedor" />
              </SelectTrigger>
              <SelectContent>
                {Object.entries(LLM_PROVIDERS).map(([provider, value]) => (
                  <SelectItem key={value} value={value}>
                    {provider}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <Label htmlFor="llm-model">Modelo</Label>
            <Input
              id="llm-model"
              placeholder="Digite o nome do modelo"
              value={settings.mem0?.llm?.config?.model || ""}
              onChange={(e) => handleLlmConfigChange("model", e.target.value)}
            />
          </div>

          {isLlmOllama && (
            <div className="space-y-2">
              <Label htmlFor="llm-ollama-url">Ollama Base URL</Label>
              <Input
                id="llm-ollama-url"
                placeholder="http://host.docker.internal:11434"
                value={settings.mem0?.llm?.config?.ollama_base_url || ""}
                onChange={(e) => handleLlmConfigChange("ollama_base_url", e.target.value)}
              />
              <p className="text-xs text-muted-foreground mt-1">
                Deixe em branco para usar o padrão: http://host.docker.internal:11434
              </p>
            </div>
          )}

          {usesLlmOpenAiCompatibleUrl && (
            <div className="space-y-2">
              <Label htmlFor="llm-openai-url">URL base da API</Label>
              <Input
                id="llm-openai-url"
                placeholder="http://host.docker.internal:8000/v1"
                value={settings.mem0?.llm?.config?.openai_base_url || ""}
                onChange={(e) => handleLlmConfigChange("openai_base_url", e.target.value)}
              />
              <p className="text-xs text-muted-foreground mt-1">
                Servidor local compatível com OpenAI (llama.cpp, LM Studio). Obrigatório com{" "}
                <strong>MEM0_LOCAL_ONLY</strong> — sem esta URL o LLM é bloqueado como nuvem.
              </p>
            </div>
          )}

          {needsLlmApiKey && (
            <div className="space-y-2">
              <Label htmlFor="llm-api-key">Chave de API</Label>
              <div className="relative">
                <Input
                  id="llm-api-key"
                  type={showLlmApiKey ? "text" : "password"}
                  placeholder="env:API_KEY"
                  value={settings.mem0?.llm?.config?.api_key || ""}
                  onChange={(e) =>
                    handleLlmConfigChange(
                      "api_key",
                      nextSecretInput(settings.mem0?.llm?.config?.api_key, e.target.value),
                    )
                  }
                />
                <Button 
                  variant="ghost" 
                  size="icon" 
                  type="button" 
                  className="absolute right-2 top-1/2 transform -translate-y-1/2 h-7 w-7"
                  onClick={() => setShowLlmApiKey(!showLlmApiKey)}
                >
                  {showLlmApiKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </Button>
              </div>
              <p className="text-xs text-muted-foreground mt-1">
                Use "env:API_KEY" para carregar de variável de ambiente, ou informe diretamente
              </p>
              {isMaskedSecret(settings.mem0?.llm?.config?.api_key) && (
                <div className="flex items-center justify-between gap-2">
                  <p className="text-xs text-muted-foreground" data-testid="llm-api-key-masked-hint">
                    Chave salva (mascarada). Mantenha, ou apague o campo, para preservar a atual; digite uma nova para trocar.
                  </p>
                  <Button
                    variant="ghost"
                    size="sm"
                    type="button"
                    className="h-7 text-xs"
                    data-testid="llm-api-key-clear"
                    onClick={() => handleLlmConfigChange("api_key", CLEARED_SECRET)}
                  >
                    Remover chave
                  </Button>
                </div>
              )}
            </div>
          )}

          <div className="flex items-center space-x-2 pt-2">
            <Switch id="llm-advanced-settings" checked={showLlmAdvanced} onCheckedChange={setShowLlmAdvanced} />
            <Label htmlFor="llm-advanced-settings">Exibir configurações avançadas</Label>
          </div>

          {showLlmAdvanced && (
            <div className="space-y-6 pt-2">
              <div className="space-y-2">
                <div className="flex justify-between">
                  <Label htmlFor="temperature">Temperatura: {settings.mem0?.llm?.config?.temperature}</Label>
                </div>
                <Slider
                  id="temperature"
                  min={0}
                  max={1}
                  step={0.1}
                  value={[settings.mem0?.llm?.config?.temperature || 0.7]}
                  onValueChange={(value) => handleLlmConfigChange("temperature", value[0])}
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="max-tokens">Máximo de tokens</Label>
                <Input
                  id="max-tokens"
                  type="number"
                  placeholder="2000"
                  value={settings.mem0?.llm?.config?.max_tokens || ""}
                  onChange={(e) => handleLlmConfigChange("max_tokens", Number.parseInt(e.target.value) || "")}
                />
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Configurações do embedder */}
      <Card>
        <CardHeader>
          <CardTitle>Configurações do embedder</CardTitle>
          <CardDescription>Configure your Embedding Modelo provider and settings</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="space-y-2">
            <Label htmlFor="embedder-provider">Provedor de embedder</Label>
            <Select 
              value={settings.mem0?.embedder?.provider || ""} 
              onValueChange={handleEmbedderProviderChange}
            >
              <SelectTrigger id="embedder-provider">
                <SelectValue placeholder="Selecione um provedor" />
              </SelectTrigger>
              <SelectContent>
                {Object.entries(EMBEDDER_PROVIDERS).map(([provider, value]) => (
                  <SelectItem key={value} value={value}>
                    {provider}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <Label htmlFor="embedder-model">Modelo</Label>
            <Input
              id="embedder-model"
              placeholder="Digite o nome do modelo"
              value={settings.mem0?.embedder?.config?.model || ""}
              onChange={(e) => handleEmbedderConfigChange("model", e.target.value)}
            />
          </div>

          {isEmbedderOllama && (
            <div className="space-y-2">
              <Label htmlFor="embedder-ollama-url">Ollama Base URL</Label>
              <Input
                id="embedder-ollama-url"
                placeholder="http://host.docker.internal:11434"
                value={settings.mem0?.embedder?.config?.ollama_base_url || ""}
                onChange={(e) => handleEmbedderConfigChange("ollama_base_url", e.target.value)}
              />
              <p className="text-xs text-muted-foreground mt-1">
                Deixe em branco para usar o padrão: http://host.docker.internal:11434
              </p>
            </div>
          )}

          {usesEmbedderOpenAiCompatibleUrl && (
            <div className="space-y-2">
              <Label htmlFor="embedder-openai-url">URL base da API</Label>
              <Input
                id="embedder-openai-url"
                placeholder="http://host.docker.internal:8000/v1"
                value={settings.mem0?.embedder?.config?.openai_base_url || ""}
                onChange={(e) => handleEmbedderConfigChange("openai_base_url", e.target.value)}
              />
              <p className="text-xs text-muted-foreground mt-1">
                Endpoint de embeddings OpenAI-compatível (llama.cpp / LM Studio).
              </p>
            </div>
          )}

          {needsEmbedderApiKey && (
            <div className="space-y-2">
              <Label htmlFor="embedder-api-key">Chave de API</Label>
              <div className="relative">
                <Input
                  id="embedder-api-key"
                  type={showEmbedderApiKey ? "text" : "password"}
                  placeholder="env:API_KEY"
                  value={settings.mem0?.embedder?.config?.api_key || ""}
                  onChange={(e) =>
                    handleEmbedderConfigChange(
                      "api_key",
                      nextSecretInput(settings.mem0?.embedder?.config?.api_key, e.target.value),
                    )
                  }
                />
                <Button 
                  variant="ghost" 
                  size="icon" 
                  type="button" 
                  className="absolute right-2 top-1/2 transform -translate-y-1/2 h-7 w-7"
                  onClick={() => setShowEmbedderApiKey(!showEmbedderApiKey)}
                >
                  {showEmbedderApiKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </Button>
              </div>
              <p className="text-xs text-muted-foreground mt-1">
                Use "env:API_KEY" para carregar de variável de ambiente, ou informe diretamente
              </p>
              {isMaskedSecret(settings.mem0?.embedder?.config?.api_key) && (
                <div className="flex items-center justify-between gap-2">
                  <p className="text-xs text-muted-foreground" data-testid="embedder-api-key-masked-hint">
                    Chave salva (mascarada). Mantenha, ou apague o campo, para preservar a atual; digite uma nova para trocar.
                  </p>
                  <Button
                    variant="ghost"
                    size="sm"
                    type="button"
                    className="h-7 text-xs"
                    data-testid="embedder-api-key-clear"
                    onClick={() => handleEmbedderConfigChange("api_key", CLEARED_SECRET)}
                  >
                    Remover chave
                  </Button>
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Backup (Exportar / Importar) */}
      <Card>
        <CardHeader>
          <CardTitle>Backup</CardTitle>
          <CardDescription>Exportare ou importe suas memórias</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {/* Exportar Section */}
          <div className="p-4 border border-zinc-800 rounded-lg space-y-2">
            <div className="text-sm font-medium">Exportar</div>
            <p className="text-xs text-muted-foreground">Baixe um ZIP contendo suas memórias.</p>
            <div>
              <Button
                type="button"
                className="bg-zinc-800 hover:bg-zinc-700"
                onClick={async () => {
                  try {
                    const res = await fetch(`${getApiUrl()}/api/v1/backup/export`, {
                      method: "POST",
                      headers: { "Content-Type": "application/json", Accept: "application/zip" },
                      body: JSON.stringify({ user_id: userId }),
                    })
                    if (!res.ok) throw new Error(`Exportar failed with status ${res.status}`)
                    const blob = await res.blob()
                    const url = window.URL.createObjectURL(blob)
                    const a = document.createElement("a")
                    a.href = url
                    a.download = `memories_export.zip`
                    document.body.appendChild(a)
                    a.click()
                    a.remove()
                    window.URL.revokeObjectURL(url)
                  } catch (e) {
                    console.error(e)
                    alert("Exportar failed. Check console for details.")
                  }
                }}
              >
                <Download className="h-4 w-4 mr-2" /> Exportar memórias
              </Button>
            </div>
          </div>

          {/* Importar Section */}
          <div className="p-4 border border-zinc-800 rounded-lg space-y-2">
            <div className="text-sm font-medium">Importar</div>
            <p className="text-xs text-muted-foreground">Envie um ZIP exportado pelo {APP_NAME}. As configurações padrão serão usadas.</p>
            <div className="flex items-center gap-3 flex-wrap">
              <input
                ref={fileInputRef}
                type="file"
                accept=".zip"
                className="hidden"
                onChange={(evt) => {
                  const f = evt.target.files?.[0]
                  if (!f) return
                  setSelectedImportarFileName(f.name)
                }}
              />
              <Button
                type="button"
                className="bg-zinc-800 hover:bg-zinc-700"
                onClick={() => {
                  if (fileInputRef.current) fileInputRef.current.click()
                }}
              >
                <Upload className="h-4 w-4 mr-2" /> Escolher ZIP
              </Button>
              <span className="text-xs text-muted-foreground truncate max-w-[220px]">
                {selectedImportarFileName || "Nenhum arquivo selecionado"}
              </span>
              <div className="ml-auto">
                <Button
                  type="button"
                  disabled={isUploading || !fileInputRef.current}
                  className="bg-primary hover:bg-primary/80 disabled:opacity-50"
                  onClick={async () => {
                    const file = fileInputRef.current?.files?.[0]
                    if (!file) return
                    try {
                      setIsUploading(true)
                      const form = new FormData()
                      form.append("file", file)
                      form.append("user_id", String(userId))
                      const res = await fetch(`${getApiUrl()}/api/v1/backup/import`, { method: "POST", body: form })
                      if (!res.ok) throw new Error(`Importar failed with status ${res.status}`)
                      await res.json()
                      if (fileInputRef.current) fileInputRef.current.value = ""
                      setSelectedImportarFileName("")
                    } catch (e) {
                      console.error(e)
                      alert("Importar failed. Check console for details.")
                    } finally {
                      setIsUploading(false)
                    }
                  }}
                >
                  {isUploading ? "Enviando..." : "Importar"}
                </Button>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  )
} 