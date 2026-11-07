import z from '@deepseek-ai/schemastery'
import { fileURLToPath } from 'node:url'
import { mountService } from 'dsh-live-studio/launcher.mjs'
export const name='dsh-game-agent'
export const inject=['connection']
export const Config=z.object({enabled:z.boolean().default(false),home:z.string().default(''),
  persona:z.string().default(''),python:z.string().default('python'),port:z.number().default(3098),
  jingyuUrl:z.string().default(''),model:z.string().default(''),pet:z.string().default(''),
  room:z.number().default(0),credential:z.string().default(''),ledger:z.string().default(''),
  gameSkill:z.string().default(''),experience:z.string().default(''),channel:z.string().default('1')})
export function apply(ctx,config){mountService(ctx,config,name,fileURLToPath(new URL('./backend',import.meta.url)))}
