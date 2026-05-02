import { useEffect, useState } from 'react'
import {
    PUBLIC_MODEL_CATALOG_FALLBACK,
    getDefaultImageModel,
    getDefaultTextModel,
    getPublicModels,
    isImageCapableModel,
    isTextCapableModel,
} from '../api/client'

export function usePublicModels() {
    const [models, setModels] = useState(PUBLIC_MODEL_CATALOG_FALLBACK)
    const [loading, setLoading] = useState(true)

    useEffect(() => {
        let cancelled = false

        async function load() {
            setLoading(true)
            const next = await getPublicModels()
            if (!cancelled) {
                setModels(next)
                setLoading(false)
            }
        }

        load()
        return () => {
            cancelled = true
        }
    }, [])

    const publicAliasPriority = {
        opus: 0,
        sonnet: 1,
        haiku: 2,
        'claude-opus-4-7': 3,
        'claude-sonnet-4-6': 4,
        'claude-haiku-4-5': 5,
    }
    const publicModelCompare = (a, b) => {
        const aRank = publicAliasPriority[a.id]
        const bRank = publicAliasPriority[b.id]
        if (aRank !== undefined || bRank !== undefined) {
            return (aRank ?? 1000) - (bRank ?? 1000)
        }
        const aIsInternalProviderAlias = /^(gpt-|gemini-|vertex-)/.test(a.id || '')
        const bIsInternalProviderAlias = /^(gpt-|gemini-|vertex-)/.test(b.id || '')
        if (aIsInternalProviderAlias !== bIsInternalProviderAlias) return aIsInternalProviderAlias ? 1 : -1
        return 0
    }
    const isUserFacingAlias = (model) => !/^(gpt-|vertex-)/.test(model?.id || '')

    const textModels = [...models.filter(isTextCapableModel).filter(isUserFacingAlias)].sort(publicModelCompare)
    const imageModels = [...models.filter(isImageCapableModel).filter(isUserFacingAlias)].sort(publicModelCompare)
    const defaultTextModel = getDefaultTextModel(textModels) || getDefaultTextModel(models)
    const defaultImageModel = getDefaultImageModel(imageModels) || getDefaultImageModel(models)

    return {
        models,
        textModels,
        imageModels,
        defaultTextModel,
        defaultImageModel,
        loading,
    }
}
