import groupProjectsForHome from './group-projects-for-home';
import { ProjectGroups } from '../constants/Enums';

const project = (id, extra = {}) => ({ id, ownerProjectManager: null, ...extra });

describe('groupProjectsForHome', () => {
  const input = {
    managerProjectModels: [project('m1'), project('own', { ownerProjectManager: { id: 'pm' } })],
    membershipProjectModels: [project('s1'), project('s2', { isCompleted: true })],
    adminProjectModels: [project('a1', { isArchived: true })],
  };

  test('modo ShareMem: tudo em Equipe, sem Compartilhados comigo/Outros', () => {
    const result = groupProjectsForHome(input, { isMem0Shared: true });

    expect(result[ProjectGroups.TEAM]).toEqual(['m1', 's1', 's2', 'a1']);
    expect(result[ProjectGroups.MY_OWN]).toEqual(['own']);
    expect(result[ProjectGroups.SHARED_WITH_ME]).toEqual([]);
    expect(result[ProjectGroups.OTHERS]).toEqual([]);
    expect(result.teamActiveIds).toEqual(['m1', 's1']);
    expect(result.teamCompletedIds).toEqual(['s2']);
    expect(result.teamArchivedIds).toEqual(['a1']);
  });

  test('modo upstream: mantém as seções originais', () => {
    const result = groupProjectsForHome(input);

    expect(result[ProjectGroups.TEAM]).toEqual(['m1']);
    expect(result[ProjectGroups.SHARED_WITH_ME]).toEqual(['s1', 's2']);
    expect(result[ProjectGroups.OTHERS]).toEqual(['a1']);
  });
});
