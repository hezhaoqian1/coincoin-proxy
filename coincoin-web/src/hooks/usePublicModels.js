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

    const textModels = models.filter(isTextCapableModel)
    const imageModels = models.filter(isImageCapableModel)
    const defaultTextModel = getDefaultTextModel(models)
    const defaultImageModel = getDefaultImageModel(models)

    return {
        models,
        textModels,
        imageModels,
        defaultTextModel,
        defaultImageModel,
        loading,
    }
}
